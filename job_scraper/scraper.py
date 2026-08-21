#!/usr/bin/env python3
"""
Early-grad job alert scraper.

Polls company career pages (Greenhouse ATS) directly — the source of truth —
and sends a Discord notification the moment a new early-grad role appears.

Runs every 5 minutes via GitHub Actions so it works even when your laptop is off.

State: seen_jobs.json (job-id keyed, 90-day auto-expiry)
Notify: Discord webhook embed per new job
"""

import json
import re
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

REQUEST_TIMEOUT = 12
SEEN_EXPIRY_DAYS = 90

EARLY_GRAD_KEYWORDS = [
    "new grad", "new graduate", "entry level", "entry-level",
    "junior", "early career", "early-career",
    "intern", "internship", "co-op", "coop",
    "university", "campus", "graduate program",
    "associate engineer", "engineer i ", "engineer 1",
    "recent grad", "fresh grad",
]

# Discord embed colour (Anthropic purple-ish)
EMBED_COLOR = 0x5865F2

# ── state ─────────────────────────────────────────────────────────────────────

def load_seen() -> dict[str, str]:
    if not SEEN_FILE.exists():
        return {}
    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        seen: dict[str, str] = raw.get("seen", {})
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_EXPIRY_DAYS)
    return {k: v for k, v in seen.items()
            if datetime.fromisoformat(v) > cutoff}


def save_seen(seen: dict[str, str]) -> None:
    SEEN_FILE.write_text(
        json.dumps({"seen": seen}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

# ── Greenhouse adapter ────────────────────────────────────────────────────────

def _get(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "job-alert-bot/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} → {url}")
        return None
    except Exception as e:
        print(f"  Error ({e.__class__.__name__}) → {url}")
        return None


def fetch_greenhouse(slug: str, company_name: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
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
    posted   = job["updated"][:10] if job["updated"] else "—"

    payload = {
        "embeds": [{
            "title":       f"{title}",
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

    for entry in config.get("greenhouse", []):
        name = entry["name"]
        slug = entry["slug"]
        jobs = fetch_greenhouse(slug, name)
        new  = [j for j in jobs if j["id"] not in seen and is_early_grad(j)]
        print(f"{name:<20} {len(jobs):>4} total  {len(new):>3} new")

        for job in new:
            seen[job["id"]] = now
            _send_discord(job)
            total_new += 1

    save_seen(seen)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[{ts}] Done — {total_new} new job(s) notified")


if __name__ == "__main__":
    main()
