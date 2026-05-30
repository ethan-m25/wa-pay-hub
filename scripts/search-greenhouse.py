#!/usr/bin/env python3
"""
wa-pay-hub/scripts/search-greenhouse.py
Greenhouse job board scraper — Washington State edition.

WA RCW 49.58.110 requires salary range + benefits disclosure.
Most Seattle tech companies are on Greenhouse.

Run: python3 ~/wa-pay-hub/scripts/search-greenhouse.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    load_existing_urls,
    write_job, TODAY, OUTPUT_FILE, WA_TERMS,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/wa-pay-hub/scripts/greenhouse.log")
LOCK_FILE = os.path.expanduser("~/wa-pay-hub/scripts/.greenhouse.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

# === Phase 4 seed loader (added 2026-05-27) ===
sys.path.insert(0, os.path.expanduser('~/shared-scripts'))
from hub_employer_seeds import load_greenhouse_seeds
SEED_SLUGS = load_greenhouse_seeds('wa')


SALARY_PATTERNS = [
    r'\$\s*([\d,]+)\s*[-–—]\s*\$\s*([\d,]+)',
    r'([\d,]+)\s*[-–—]\s*([\d,]+)\s*(?:USD|usd)',
    r'salary[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'compensation[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'pay range[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'"salary_min":\s*(\d+).*?"salary_max":\s*(\d+)',
    r'"min_salary":\s*(\d+).*?"max_salary":\s*(\d+)',
]


def parse_salary_from_text(text: str):
    if not text:
        return None, None
    text = html_mod.unescape(html_mod.unescape(text))
    for pat in SALARY_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                val_min = int(float(raw_min))
                val_max = int(float(raw_max))
                if val_min < 1000:
                    val_min *= 1000
                if val_max < 1000:
                    val_max *= 1000
                if 30_000 <= val_min < val_max <= 1_500_000:
                    return val_min, val_max
            except (ValueError, IndexError):
                continue
    return None, None



_CANADA_EXCL = [
    "british columbia", "ontario, canada", "alberta, canada", "quebec, canada",
    "toronto", "vancouver", "montreal", "calgary", "ottawa", "edmonton",
    ", canada", "canada,", "remote - canada", "remote - alberta",
    "remote - ontario", "remote - british columbia", "remote - quebec",
]

_REMOTE_TERMS = ("remote", "distributed", "virtual", "anywhere", "work from", "wfh")

def is_wa_job(title: str, location: str, content: str) -> bool:
    loc_low = location.lower()
    if any(t in loc_low for t in _CANADA_EXCL):
        return False
    # Exclude DC explicitly before "washington" check
    if "washington, dc" in loc_low or "washington dc" in loc_low:
        return False
    if any(t in loc_low for t in WA_TERMS):
        return True
    # Remote/unspecified jobs are WA-eligible
    if not loc_low or any(r in loc_low for r in _REMOTE_TERMS):
        return True
    return False


def fetch_company_jobs(slug: str, company_name_override=None):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        resp = fetcher.get(url, timeout=20)
        data = resp.json()
    except Exception as e:
        log(f"  [{slug}] API error: {e}")
        return []

    jobs_raw = data.get("jobs", [])
    if not jobs_raw:
        return []

    company_name = company_name_override or data.get("company", {}).get("name") or slug.title()
    results = []

    for j in jobs_raw:
        updated_at = j.get("updated_at", "")
        if updated_at and updated_at < LOOKBACK_DATE:
            continue

        title = j.get("title", "").strip()
        location_obj = j.get("location", {})
        location = location_obj.get("name", "") if isinstance(location_obj, dict) else str(location_obj)
        content_html = j.get("content", "")
        content_text = re.sub(r'<[^>]+>', ' ', content_html)
        content_text = html_mod.unescape(content_text)

        if not is_wa_job(title, location, content_text):
            continue

        val_min, val_max = parse_salary_from_text(content_html + " " + content_text)
        if val_min is None:
            val_min, val_max = parse_salary_from_text(str(j))

        if val_min is None:
            continue

        posted_date = updated_at[:10] if updated_at else TODAY
        job_url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j.get('id','')}"

        results.append({
            "role": title,
            "company": company_name,
            "min": val_min,
            "max": val_max,
            "location": location or "Seattle, WA",
            "source_url": job_url,
            "posted": posted_date,
            "source_platform": "greenhouse",
        })

    return results


def main():
    if not acquire_lock(LOCK_FILE, log):
        return

    log("=== WA Greenhouse scraper started ===")
    existing = load_existing_keys()
    existing_urls = load_existing_urls()
    log(f"Existing dedup keys: {len(existing)}")

    new_count = 0
    for slug, name_override in SEED_SLUGS:
        log(f"[{slug}] fetching...")
        jobs = fetch_company_jobs(slug, name_override)
        for job in jobs:
            if job.get("source_url") in existing_urls:
                continue
            key = f"{job['role'].lower().strip()}|{job['company'].lower().strip()}"
            if key in existing:
                continue
            write_job(OUTPUT_FILE, job)
            existing.add(key)
            existing_urls.add(job.get("source_url", ""))
            new_count += 1
            log(f"  + {job['role']} @ {job['company']} | ${job['min']:,}–${job['max']:,} | {job['location']}")
        time.sleep(0.5)

    log(f"=== Done. {new_count} new WA jobs written to {OUTPUT_FILE} ===")


if __name__ == "__main__":
    main()
