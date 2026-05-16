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
    write_job, TODAY, OUTPUT_FILE, WA_TERMS,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/wa-pay-hub/scripts/greenhouse.log")
LOCK_FILE = os.path.expanduser("~/wa-pay-hub/scripts/.greenhouse.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SEED_SLUGS = [
    # ── Seattle Big Tech ──────────────────────────────────────────────────────
    ("amazon", None),              # Amazon, Seattle HQ
    ("microsoft", None),           # Microsoft, Redmond HQ
    ("google", None),              # Google, Kirkland/Seattle
    ("meta", None),                # Meta, Seattle office
    ("apple", None),               # Apple, Seattle
    ("salesforce", None),          # Salesforce, Seattle
    # ── Seattle Native Tech ───────────────────────────────────────────────────
    ("expedia", None),             # Expedia, Seattle HQ
    ("zillow", None),              # Zillow, Seattle HQ
    ("redfin", None),              # Redfin, Seattle HQ
    ("outreach", None),            # Outreach, Seattle HQ
    ("convoy", None),              # Convoy, Seattle
    ("avalara", None),             # Avalara, Seattle
    ("accolade", None),            # Accolade, Seattle
    ("remitly", None),             # Remitly, Seattle HQ
    ("rover", None),               # Rover, Seattle HQ
    ("zulily", None),              # Zulily, Seattle
    ("apptio", None),              # Apptio, Bellevue
    ("pushpay", None),             # Pushpay, Redmond
    ("isilon", None),              # Dell EMC Isilon, Seattle
    ("formstack", None),           # Formstack, remote/WA
    # ── Bellevue / Eastside ───────────────────────────────────────────────────
    ("concur", None),              # SAP Concur, Bellevue
    ("smartsheet", None),          # Smartsheet, Bellevue HQ
    ("tableau", None),             # Tableau/Salesforce, Bellevue
    ("valve", None),               # Valve, Bellevue
    ("nintex", None),              # Nintex, Bellevue
    # ── Aerospace / Defense ───────────────────────────────────────────────────
    ("boeing", None),              # Boeing, Seattle/Renton HQ
    ("blueorigin", None),          # Blue Origin, Kent/Seattle
    ("spacex", None),              # SpaceX, Redmond (Starlink)
    # ── Healthcare / Biotech ──────────────────────────────────────────────────
    ("seagen", None),              # Seagen (now Pfizer), Bothell
    ("iqvia", None),               # IQVIA, Seattle
    ("providence", None),          # Providence Health, Renton
    # ── Retail / Starbucks ────────────────────────────────────────────────────
    ("starbucks", None),           # Starbucks, Seattle HQ
    ("nordstrom", None),           # Nordstrom, Seattle HQ
    ("costco", None),              # Costco, Issaquah HQ
    ("rei", None),                 # REI, Bellevue/Kent
    ("zulily", None),              # Zulily, Seattle
    # ── Gaming ────────────────────────────────────────────────────────────────
    ("bungie", None),              # Bungie, Bellevue HQ
    ("pocketgems", None),          # Pocket Gems, Seattle
    ("popupstudios", None),
    # ── Fintech / Finance ─────────────────────────────────────────────────────
    ("stripe", None),              # Stripe, Seattle office
    ("robinhoodmarkets", None),    # Robinhood, remote/WA
    ("plaid", None),               # Plaid, Seattle office
    ("navan", None),               # Navan, remote/WA
    # ── Cloud / Infrastructure ───────────────────────────────────────────────
    ("hashicorp", None),           # HashiCorp, remote/Seattle
    ("puppet", None),              # Puppet, Portland/Seattle area
    ("databricks", None),          # Databricks, Seattle office
    ("confluent", None),           # Confluent, Seattle
    ("snowflake", None),           # Snowflake, Seattle
    ("datadog", None),             # Datadog, Seattle
    ("cloudflare", None),          # Cloudflare, Seattle
    ("newrelic", None),            # New Relic, remote/WA
]


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

def is_wa_job(title: str, location: str, content: str) -> bool:
    loc_low = location.lower()
    if any(t in loc_low for t in _CANADA_EXCL):
        return False
    combined = f"{title} {location} {content}".lower()
    return any(term in combined for term in WA_TERMS)


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
    log(f"Existing dedup keys: {len(existing)}")

    new_count = 0
    for slug, name_override in SEED_SLUGS:
        log(f"[{slug}] fetching...")
        jobs = fetch_company_jobs(slug, name_override)
        for job in jobs:
            key = f"{job['role'].lower().strip()}|{job['company'].lower().strip()}"
            if key in existing:
                continue
            write_job(OUTPUT_FILE, job)
            existing.add(key)
            new_count += 1
            log(f"  + {job['role']} @ {job['company']} | ${job['min']:,}–${job['max']:,} | {job['location']}")
        time.sleep(0.5)

    log(f"=== Done. {new_count} new WA jobs written to {OUTPUT_FILE} ===")


if __name__ == "__main__":
    main()
