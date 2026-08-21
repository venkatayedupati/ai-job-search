#!/usr/bin/env python3
"""
Automated scraper for early grad / entry-level software engineer roles in the US.

Polls the freehire.dev public API every run, deduplicates against seen_jobs.json,
and sends a Gmail digest for each batch of new postings.

Required environment variables:
    GMAIL_FROM          your Gmail address        (e.g. you@gmail.com)
    GMAIL_APP_PASSWORD  16-char Gmail app password
    GMAIL_TO            recipient (defaults to GMAIL_FROM if omitted)

Setup: see job_scraper/README_SCRAPER.md
"""

import json
import os
import re
import smtplib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
SEEN_FILE = SCRIPT_DIR / "seen_jobs.json"
LOG_FILE = SCRIPT_DIR / "scraper.log"

# ── config ────────────────────────────────────────────────────────────────────

FREEHIRE_BASE = "https://freehire.dev"
REQUEST_TIMEOUT = 20
MAX_PER_SEARCH = 50
SEEN_EXPIRY_DAYS = 60   # prune slugs older than this to keep the file small

# Title keywords that confirm an early-grad / entry-level intent.
# The API seniority filter is a bonus hint but not reliable — some ATS don't tag it.
EARLY_GRAD_KEYWORDS = {
    "new grad", "new graduate", "entry level", "entry-level",
    "junior", "associate swe", "associate software", "associate engineer",
    "early career", "early-career", "fresh grad", "recent grad",
    "graduate engineer", "grad engineer", "grad swe",
    "intern", "internship",
}

# Queries sent to freehire on each run.  countries=us scopes to US postings.
SEARCHES = [
    {"q": "software engineer new grad",   "countries": "us", "posted_within_days": "3"},
    {"q": "new grad software engineer",   "countries": "us", "posted_within_days": "3"},
    {"q": "entry level software engineer","countries": "us", "posted_within_days": "3"},
    {"q": "software engineer intern",     "countries": "us", "posted_within_days": "3"},
    {"q": "junior software engineer",     "countries": "us", "posted_within_days": "3"},
]

# ── logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ── state management ──────────────────────────────────────────────────────────

def load_seen() -> dict[str, str]:
    """Load slug → ISO-timestamp map from disk; prune entries older than SEEN_EXPIRY_DAYS."""
    if not SEEN_FILE.exists():
        return {}
    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        seen: dict[str, str] = raw.get("seen", {})
    except (json.JSONDecodeError, OSError):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_EXPIRY_DAYS)
    pruned = {
        slug: ts for slug, ts in seen.items()
        if datetime.fromisoformat(ts) > cutoff
    }
    return pruned


def save_seen(seen: dict[str, str]) -> None:
    SEEN_FILE.write_text(
        json.dumps({"seen": seen}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── freehire API ──────────────────────────────────────────────────────────────

def fetch_jobs(params: dict) -> list[dict]:
    """Fetch one page of search results from freehire; returns [] on any error."""
    full_params = {
        **params,
        "limit": str(MAX_PER_SEARCH),
        "offset": "0",
        "semantic_ratio": "0",
    }
    qs = urllib.parse.urlencode(full_params)
    url = f"{FREEHIRE_BASE}/api/v1/jobs/search?{qs}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ai-job-search-scraper/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data") or []
    except urllib.error.HTTPError as exc:
        log(f"WARNING: HTTP {exc.code} from freehire for params {params}")
        return []
    except Exception as exc:
        log(f"WARNING: fetch failed ({exc.__class__.__name__}: {exc})")
        return []


SWE_TITLE_KEYWORDS = {
    "software engineer", "software developer", "swe", "backend engineer",
    "frontend engineer", "full stack engineer", "fullstack engineer",
    "full-stack engineer", "platform engineer", "infrastructure engineer",
    "ml engineer", "machine learning engineer", "data engineer",
    "devops engineer", "site reliability engineer", "sre",
    "systems engineer", "embedded engineer", "firmware engineer",
    "mobile engineer", "ios engineer", "android engineer",
    "software development", "engineer i", "engineer ii",
}


def is_swe_role(job: dict) -> bool:
    """Return True when the job title looks like a software engineering role."""
    title = (job.get("title") or "").lower()
    if any(kw in title for kw in SWE_TITLE_KEYWORDS):
        return True
    # freehire category tag is a reliable secondary signal
    category = ((job.get("enrichment") or {}).get("category") or "").lower()
    return category in ("backend", "frontend", "fullstack", "devops", "ml_ai", "mobile", "systems")


def _word_in(phrase: str, text: str) -> bool:
    """Return True when `phrase` appears as a whole word (not a substring) in `text`."""
    return bool(re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", text))


def is_early_grad(job: dict) -> bool:
    """Return True when the job title or seniority tag marks it as entry-level."""
    title = (job.get("title") or "").lower()
    if any(_word_in(kw, title) for kw in EARLY_GRAD_KEYWORDS):
        return True
    seniority = ((job.get("enrichment") or {}).get("seniority") or "").lower()
    return seniority in ("junior", "intern", "entry")


# ── email notification ────────────────────────────────────────────────────────

def _job_html_row(job: dict) -> str:
    title   = job.get("title") or "(no title)"
    company = job.get("company") or "Unknown company"
    loc     = job.get("location") or "US / Remote"
    date    = (job.get("posted_at") or job.get("created_at") or "")[:10] or "—"
    url     = job.get("url") or "#"
    snty    = ((job.get("enrichment") or {}).get("seniority") or "").capitalize()
    skills  = ", ".join((job.get("skills") or [])[:5])
    meta    = " · ".join(filter(None, [company, loc, date, snty]))
    skill_line = f"<br><small style='color:#888'>Skills: {skills}</small>" if skills else ""
    return (
        f"<tr><td style='padding:10px 4px;border-bottom:1px solid #eee'>"
        f"<a href='{url}' style='font-weight:bold;color:#1a73e8;text-decoration:none'>{title}</a><br>"
        f"<span style='color:#555;font-size:0.9em'>{meta}</span>{skill_line}"
        f"</td></tr>"
    )


def _job_text_block(job: dict) -> str:
    title   = job.get("title") or "(no title)"
    company = job.get("company") or "Unknown company"
    loc     = job.get("location") or "US / Remote"
    date    = (job.get("posted_at") or job.get("created_at") or "")[:10] or "—"
    url     = job.get("url") or ""
    return f"• {title}\n  {company} · {loc} · {date}\n  {url}"


def send_email(jobs: list[dict]) -> None:
    sender    = os.environ.get("GMAIL_FROM", "").strip()
    password  = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient = os.environ.get("GMAIL_TO", "").strip() or sender

    if not sender or not password:
        log("WARNING: GMAIL_FROM/GMAIL_APP_PASSWORD not set — skipping email (jobs logged below)")
        for j in jobs:
            log(f"  NEW JOB: {j.get('title')} @ {j.get('company')} — {j.get('url')}")
        return

    subject = f"[Job Alert] {len(jobs)} new early-grad SWE role{'s' if len(jobs) > 1 else ''}"

    text_body = f"Found {len(jobs)} new early grad software engineering role(s):\n\n"
    text_body += "\n\n".join(_job_text_block(j) for j in jobs)
    text_body += "\n\n—\nai-job-search scraper · powered by freehire.dev"

    html_rows = "".join(_job_html_row(j) for j in jobs)
    html_body = f"""<html><body style='font-family:Arial,sans-serif;color:#333;max-width:640px;margin:auto'>
<h2 style='color:#1a73e8;margin-bottom:4px'>New Early-Grad SWE Roles</h2>
<p style='color:#555;margin-top:0'>{len(jobs)} new posting(s) found</p>
<table width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse'>
{html_rows}
</table>
<p style='color:#aaa;font-size:0.8em;margin-top:20px'>ai-job-search scraper · powered by freehire.dev</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
        log(f"Email sent to {recipient} ({len(jobs)} job(s))")
    except Exception as exc:
        log(f"ERROR: email failed — {exc.__class__.__name__}: {exc}")


def mac_notify(count: int) -> None:
    """Fire a macOS notification center banner (non-blocking, best-effort)."""
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{count} new early-grad SWE role(s) found" '
                f'with title "Job Alert" subtitle "Check your email"',
            ],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log("── scraper run start ──")
    seen = load_seen()

    new_jobs: list[dict] = []
    seen_this_run: set[str] = set()

    for params in SEARCHES:
        jobs = fetch_jobs(params)
        log(f"  query={params.get('q')!r}: {len(jobs)} result(s) from API")
        for job in jobs:
            slug = job.get("public_slug") or ""
            if not slug or slug in seen or slug in seen_this_run:
                continue
            if not is_swe_role(job) or not is_early_grad(job):
                continue
            seen_this_run.add(slug)
            new_jobs.append(job)

    if new_jobs:
        log(f"Found {len(new_jobs)} new job(s)")
        now_iso = datetime.now(timezone.utc).isoformat()
        for job in new_jobs:
            slug = job.get("public_slug") or ""
            if slug:
                seen[slug] = now_iso
        save_seen(seen)
        send_email(new_jobs)
        mac_notify(len(new_jobs))
    else:
        log("No new jobs this run")
        # Always persist the pruned seen map so old entries are cleaned up.
        save_seen(seen)

    log("── scraper run end ──")


if __name__ == "__main__":
    main()
