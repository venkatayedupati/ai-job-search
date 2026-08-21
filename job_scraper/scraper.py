#!/usr/bin/env python3
"""
Early-grad job alert scraper.

Polls company career pages directly — the source of truth —
and sends a Discord notification the moment a new early-grad role appears.

Runs every 5 minutes via GitHub Actions so it works even when your laptop is off.

State: seen_jobs.json (job-id keyed, 90-day auto-expiry)
Notify: Discord webhook embed per new job

Coverage:
  Greenhouse  — 40 companies (Figma, Stripe, Anthropic, DeepMind, …)
  Amazon      — amazon.jobs public JSON API
  Netflix     — explore.jobs.netflix.net public JSON API
  LinkedIn    — guest API proxy for Google, Meta, Apple, Microsoft,
                Nvidia, OpenAI, Notion, Snowflake (no account needed)
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────

ROOT        = Path(__file__).parent
SEEN_FILE   = ROOT / "seen_jobs.json"
CONFIG_FILE = ROOT / "companies.json"

# ── config ────────────────────────────────────────────────────────────────────

import os
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

REQUEST_TIMEOUT  = 15
SEEN_EXPIRY_DAYS = 90

EARLY_GRAD_KEYWORDS = [
    "new grad", "new graduate", "entry level", "entry-level",
    "junior", "early career", "early-career",
    "intern", "internship", "co-op", "coop",
    "university", "campus", "graduate program",
    "associate engineer", "engineer i ", "engineer 1",
    "recent grad", "fresh grad",
]

# Discord embed colour
EMBED_COLOR = 0x5865F2

# ── state ─────────────────────────────────────────────────────────────────────

def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    try:
        raw  = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        seen = raw.get("seen", {})
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_EXPIRY_DAYS)
    return {k: v for k, v in seen.items()
            if datetime.fromisoformat(v) > cutoff}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(
        json.dumps({"seen": seen}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

# ── HTTP helper ───────────────────────────────────────────────────────────────

def _get(url: str, headers: dict = None):
    default_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; job-alert-bot/1.0)",
        "Accept":     "application/json, text/html, */*",
    }
    if headers:
        default_headers.update(headers)
    req = urllib.request.Request(url, headers=default_headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} → {url}")
        return None
    except Exception as e:
        print(f"  Error ({e.__class__.__name__}) → {url}")
        return None

# ── Greenhouse adapter ────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str, company_name: str) -> list:
    url  = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    data = _get(url)
    if not data:
        return []
    jobs = []
    for j in data.get("jobs", []):
        dept = " ".join(d.get("name", "") for d in j.get("departments", []))
        jobs.append({
            "id":       f"gh:{slug}:{j['id']}",
            "title":    j.get("title", ""),
            "company":  company_name,
            "location": j.get("location", {}).get("name", ""),
            "url":      j.get("absolute_url", ""),
            "dept":     dept,
            "updated":  j.get("updated_at", ""),
            "ats":      "Greenhouse",
        })
    return jobs

# ── Amazon adapter ────────────────────────────────────────────────────────────

AMAZON_KEYWORDS = [
    "new grad", "new graduate", "intern", "internship",
    "entry level", "junior", "university", "recent grad",
]

def fetch_amazon() -> list:
    jobs   = []
    seen_ids = set()
    for kw in AMAZON_KEYWORDS:
        q   = urllib.parse.quote_plus(kw)
        url = (
            "https://www.amazon.jobs/en/search.json"
            f"?query={q}"
            "&normalized_country_code[]=USA"
            "&offset=0&result_limit=100&sort=recent"
        )
        data = _get(url)
        if not data:
            continue
        for j in data.get("jobs", []):
            jid = f"amz:{j.get('id', j.get('job_id', ''))}"
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            jobs.append({
                "id":       jid,
                "title":    j.get("title", ""),
                "company":  "Amazon",
                "location": j.get("location", ""),
                "url":      "https://www.amazon.jobs" + j.get("job_path", ""),
                "dept":     j.get("business_category", j.get("team", "")),
                "updated":  j.get("posted_date", ""),
                "ats":      "Amazon Jobs",
            })
    return jobs

# ── Netflix adapter ───────────────────────────────────────────────────────────

NETFLIX_KEYWORDS = [
    "new grad", "intern", "entry level", "junior", "university",
]

def fetch_netflix() -> list:
    jobs     = []
    seen_ids = set()
    for kw in NETFLIX_KEYWORDS:
        q   = urllib.parse.quote_plus(kw)
        url = (
            "https://explore.jobs.netflix.net/api/apply/v2/jobs"
            f"?domain=netflix.com&query={q}&limit=100"
        )
        data = _get(url)
        if not data:
            continue
        for j in data.get("positions", []):
            jid = f"nflx:{j.get('id', '')}"
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            locations = j.get("locations", [])
            loc0      = locations[0] if locations else ""
            location  = loc0.get("name", loc0) if isinstance(loc0, dict) else str(loc0)
            teams     = j.get("teams", [])
            team0     = teams[0] if teams else ""
            dept      = team0.get("name", team0) if isinstance(team0, dict) else str(team0)
            jobs.append({
                "id":       jid,
                "title":    j.get("name", ""),
                "company":  "Netflix",
                "location": location,
                "url":      f"https://explore.jobs.netflix.net/careers?pid={j.get('id','')}",
                "dept":     dept,
                "updated":  j.get("posting_date", j.get("posted_on", "")),
                "ats":      "Netflix Jobs",
            })
    return jobs

# ── LinkedIn guest API adapter ────────────────────────────────────────────────
# No login required — LinkedIn's public job-guest endpoint returns HTML snippets
# that contain structured data we can parse.

LINKEDIN_KEYWORDS = [
    "software engineer new grad",
    "software engineer intern",
    "software engineer entry level",
]

def _parse_linkedin_html(html: str, company_name: str, prefix: str) -> list:
    """Extract job cards from LinkedIn guest API HTML response."""
    jobs = []
    # Each card: <div class="base-card..." data-entity-urn="urn:li:jobPosting:NNNN"
    for match in re.finditer(r'data-entity-urn="urn:li:jobPosting:(\d+)"', html):
        job_id = match.group(1)
        jobs.append({
            "id":       f"{prefix}:{job_id}",
            "title":    _extract_between(html, match.start(), "base-search-card__title", "</h3>"),
            "company":  company_name,
            "location": _extract_between(html, match.start(), "job-search-card__location", "</span>"),
            "url":      f"https://www.linkedin.com/jobs/view/{job_id}/",
            "dept":     "",
            "updated":  _extract_between(html, match.start(), 'datetime="', '"'),
            "ats":      "LinkedIn",
        })
    return jobs


def _extract_between(html: str, start: int, open_tag: str, close_tag: str) -> str:
    """Pull inner text between two markers, searching forward from `start`."""
    window = html[start:start + 3000]
    s = window.find(open_tag)
    if s == -1:
        return ""
    s += len(open_tag)
    # Always advance past the closing > of the containing HTML tag
    gt = window.find(">", s)
    if gt != -1:
        s = gt + 1
    e = window.find(close_tag, s)
    if e == -1:
        return ""
    return re.sub(r"<[^>]+>", "", window[s:e]).strip()


def fetch_linkedin(company_id: str, company_name: str) -> list:
    jobs     = []
    seen_ids = set()
    prefix   = f"li:{company_id}"
    for kw in LINKEDIN_KEYWORDS:
        q   = urllib.parse.quote_plus(kw)
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={q}"
            f"&f_C={company_id}"
            "&location=United+States"
            "&geoId=103644278"
            "&start=0"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept":     "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                html = resp.read().decode("utf-8")
        except Exception as e:
            print(f"  LinkedIn {company_name} error: {e}")
            time.sleep(2)
            continue

        for job in _parse_linkedin_html(html, company_name, prefix):
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                jobs.append(job)
        time.sleep(1)  # respect LinkedIn rate limits
    return jobs

# ── filter ────────────────────────────────────────────────────────────────────

def _word_match(keyword: str, text: str) -> bool:
    return bool(re.search(
        r"(?<![a-z])" + re.escape(keyword) + r"(?![a-z])",
        text
    ))


def is_early_grad(job: dict) -> bool:
    haystack = (job["title"] + " " + job["dept"]).lower()
    return any(_word_match(kw, haystack) for kw in EARLY_GRAD_KEYWORDS)

# ── Discord notification ───────────────────────────────────────────────────────

def _send_discord(job: dict) -> None:
    if not DISCORD_WEBHOOK:
        print(f"  [no webhook] NEW: {job['title']} @ {job['company']}")
        return

    title    = job["title"]
    company  = job["company"]
    location = job["location"] or "Not specified"
    url      = job["url"]
    dept     = job["dept"] or "Engineering"
    updated  = job["updated"]
    posted   = updated[:10] if updated else "—"

    payload = {
        "embeds": [{
            "title":       title,
            "url":         url,
            "color":       EMBED_COLOR,
            "description": f"**{company}**",
            "fields": [
                {"name": "📍 Location",   "value": location, "inline": True},
                {"name": "🏢 Department", "value": dept,     "inline": True},
                {"name": "📅 Posted",     "value": posted,   "inline": True},
            ],
            "footer": {
                "text": f"{job['ats']} • Apply now — early applicants get prioritised"
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }

    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                print(f"  Discord returned {resp.status}")
    except Exception as e:
        print(f"  Discord error: {e}")

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    seen   = load_seen()
    now    = datetime.now(timezone.utc).isoformat()

    total_new = 0

    # Greenhouse companies
    for entry in config.get("greenhouse", []):
        name = entry["name"]
        slug = entry["slug"]
        jobs = fetch_greenhouse(slug, name)
        new  = [j for j in jobs if j["id"] not in seen and is_early_grad(j)]
        print(f"{name:<22} {len(jobs):>4} total  {len(new):>3} new  [Greenhouse]")
        for job in new:
            seen[job["id"]] = now
            _send_discord(job)
            total_new += 1

    # Amazon
    jobs = fetch_amazon()
    new  = [j for j in jobs if j["id"] not in seen and is_early_grad(j)]
    print(f"{'Amazon':<22} {len(jobs):>4} total  {len(new):>3} new  [Amazon Jobs]")
    for job in new:
        seen[job["id"]] = now
        _send_discord(job)
        total_new += 1

    # Netflix
    jobs = fetch_netflix()
    new  = [j for j in jobs if j["id"] not in seen and is_early_grad(j)]
    print(f"{'Netflix':<22} {len(jobs):>4} total  {len(new):>3} new  [Netflix Jobs]")
    for job in new:
        seen[job["id"]] = now
        _send_discord(job)
        total_new += 1

    # LinkedIn companies (MAANG + others with no public ATS API)
    for entry in config.get("linkedin", []):
        name       = entry["name"]
        company_id = str(entry["id"])
        jobs = fetch_linkedin(company_id, name)
        new  = [j for j in jobs if j["id"] not in seen and is_early_grad(j)]
        print(f"{name:<22} {len(jobs):>4} total  {len(new):>3} new  [LinkedIn]")
        for job in new:
            seen[job["id"]] = now
            _send_discord(job)
            total_new += 1

    save_seen(seen)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[{ts}] Done — {total_new} new job(s) notified")


if __name__ == "__main__":
    main()
