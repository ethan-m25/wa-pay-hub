#!/usr/bin/env python3
"""
wa-pay-hub/scripts/search-lever.py
Lever job board scraper — New York State edition.

WA RCW 49.58.110: employers with 15+ employees must post salary range + benefits.
Effective January 1, 2023.

Strategy:
  1. Seed slugs (known NY-present Lever companies) + Exa discovery
  2. Lever public postings JSON API → all jobs per company (no auth needed)
  3. Salary: structured salaryRange field first, then text regex fallback
  4. NY filter: location mentions NY / New York / NYC / remote-eligible

Run: python3 ~/wa-pay-hub/scripts/search-lever.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, exa_search, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/wa-pay-hub/scripts/lever.log")
LOCK_FILE = os.path.expanduser("~/wa-pay-hub/scripts/.lever.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SEED_SLUGS = [
    # Seattle tech / SaaS
    "convoy",           # Seattle freight marketplace
    "expedia",          # Expedia Group Seattle HQ
    "redfin",           # Redfin Seattle HQ
    "tableau",          # Tableau (Salesforce) Seattle
    "auth0",            # Auth0 (Okta) Bellevue
    "f5",               # F5 Networks Seattle
    "icertis",          # Icertis Bellevue
    "outreach",         # Outreach Seattle
    "pushpay",          # Pushpay Redmond
    "rover",            # Rover Seattle
    "remitly",          # Remitly Seattle HQ
    "smartsheet",       # Smartsheet Bellevue
    "synapse",          # Synapse Seattle
    "zipwhip",          # Zipwhip (Twilio) Seattle
    # Fintech / payments
    "accolade",
    "fundera",
    "limelight",
    "qumulo",
    "sievert",
    # Healthtech
    "accolade",
    "brightmd",
    "healthsparq",
    "iora",
    "premera",          # Premera Blue Cross WA
    "seagen",           # Seagen Bothell (oncology)
    # E-commerce / retail tech
    "chewy",
    "ignitionone",
    "lattice",
    "mercato",
    "ordermark",
    # Enterprise / cloud
    "apptio",           # Apptio Bellevue
    "avalara",          # Avalara Seattle
    "bizible",
    "brightflag",
    "concord",
    "elevate",
    "extrahop",         # ExtraHop Seattle
    "highspot",         # Highspot Seattle
    "jinni",
    "limeade",          # Limeade Bellevue
    "moz",              # Moz Seattle
    "newmarket",
    "nintex",           # Nintex Bellevue
    "outbrain",
    "payscale",         # PayScale Seattle
    "pendo",
    "percolate",
    "premera",
    "pushpay",
    "qumulo",           # Qumulo Seattle
    "samsara",
    "seeq",
    "synchroteam",
    "tableau",
    "tapcart",
    "totvs",
    "upbound",
    "vindicia",
    "walkme",
    "wavecell",
    "wildfireinteractive",
    "xero",             # Xero US (Seattle office)
    "zenreach",
    "ziprecruiter",
    # Gaming / entertainment
    "bigfish",
    "bungie",
    "moz",
    "nintendo",         # Nintendo of America Redmond
    "pokemongo",
    "valvecorporation",
    # Biotech / pharma
    "agilent",
    "blueoriginllc",
    "genoptix",
    "immunomedics",
    "integra",
    "juno",
    "lumos",
    "nanostring",
    "oncobiologics",
    "seagen",
    "ultragenyx",
    "zymeworks",
]


DISCOVERY_QUERIES = [
    'site:jobs.lever.co "Seattle" OR "Bellevue" salary 2026',
    'site:jobs.lever.co "Washington State" OR "Seattle, WA" salary range 2026',
    'site:jobs.lever.co "Seattle" engineer OR analyst OR manager salary',
    'site:jobs.lever.co "Redmond" OR "Kirkland" OR "Bothell" salary 2026',
    'site:jobs.lever.co "remote" "Washington" salary annual 2026',
    'site:jobs.lever.co Seattle tech OR biotech OR finance salary 2026',
]

WA_TERMS = [
    "washington", "seattle", "bellevue", "redmond", "kirkland", "bothell",
    "tacoma", "spokane", "renton", "kent", "everett", "federal way",
    "issaquah", "sammamish", "shoreline", "burien", ", wa", "wa,",
    "washington state",
    "remote",   # remote roles from WA-based companies under RCW 49.58.110
]

_NON_WA_LOC_TERMS = [
    "california", ", ca,", "san francisco", "los angeles",
    "new york", "nyc", ", ny,", "chicago", "boston", "austin", "texas", ", tx,",
    "florida", ", fl,", "denver", "colorado", ", co,", "washington, dc",
    "toronto", "ontario", "british columbia", "london, uk", "london, england",
]

SALARY_RE = [
    # "$120,000 - $180,000" or "$120,000 USD - $180,000 USD"
    re.compile(r'\$\s*([\d,]+)(?:\.\d+)?\s*(?:USD|usd)?\s*[-–—to]+\s*\$\s*([\d,]+)', re.IGNORECASE),
    # "$120K - $180K"
    re.compile(r'\$([\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*\$([\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
    # "salary: 80,000 to 120,000" or "base pay: $94,500 - $130,000"
    re.compile(r'(?:pay|salary|compensation|base|wage|range)[^$\n]{0,50}\$?([\d,]{5,})\s*[-–—to]+\s*\$?([\d,]{5,})', re.IGNORECASE),
]

LEVER_SLUG_RE = re.compile(r'https?://jobs\.lever\.co/([a-zA-Z0-9._-]+)', re.IGNORECASE)
_SKIP_SLUGS = {'jobs', 'search', 'home'}


def discover_slugs(seed_slugs):
    known = set(seed_slugs)
    discovered = set()
    for i, query in enumerate(DISCOVERY_QUERIES, 1):
        log(f"  Discovery Exa [{i}/{len(DISCOVERY_QUERIES)}]: {query[:60]}...")
        resp = exa_search(query, num_results=15, start_date=LOOKBACK_DATE, log=log)
        if not resp:
            continue
        new = 0
        for r in resp.get("results", []):
            m = LEVER_SLUG_RE.search(r.get("url", ""))
            if not m:
                continue
            slug = m.group(1).lower()
            if slug in _SKIP_SLUGS or slug in known or len(slug) < 2:
                continue
            discovered.add(slug)
            new += 1
        log(f"    → {new} new slugs")
        time.sleep(1.5)
    return discovered


def fetch_company_jobs(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        page = fetcher.get(url, timeout=20)
        if page.status != 200:
            return []
        return page.json() or []
    except Exception as e:
        log(f"  API error ({slug}): {e}")
        return []


def is_washington(location_str, desc_text=""):
    loc = (location_str or "").lower()
    if any(t in loc for t in _NON_WA_LOC_TERMS):
        return False
    if any(t in loc for t in WA_TERMS):
        return True
    # Lever often uses "Remote" for company-wide remote — accept if no non-NY indicator
    if "remote" in loc and not any(t in loc for t in _NON_WA_LOC_TERMS):
        return True
    return False


def parse_location(location_str):
    loc = (location_str or "").lower()
    city_map = {
        "seattle": "Seattle, WA", "bellevue": "Bellevue, WA",
        "redmond": "Redmond, WA", "kirkland": "Kirkland, WA",
        "bothell": "Bothell, WA", "tacoma": "Tacoma, WA",
        "spokane": "Spokane, WA", "renton": "Renton, WA",
        "issaquah": "Issaquah, WA",
    }
    for key, label in city_map.items():
        if key in loc:
            return label
    if "remote" in loc:
        return "Remote (WA)"
    return "Seattle, WA"


def extract_salary_from_range(sal_range):
    """Extract (min, max) from Lever's structured salaryRange field (USD annual)."""
    if not sal_range:
        return None
    currency = sal_range.get("currency", "").upper()
    if currency not in ("USD", ""):
        return None
    if sal_range.get("interval", "") != "per-year-salary":
        return None
    try:
        vmin = int(float(sal_range["min"]))
        vmax = int(float(sal_range["max"]))
        if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
            return vmin, vmax
    except (KeyError, ValueError, TypeError):
        pass
    return None


def extract_salary_from_text(text):
    if not text:
        return None
    clean = html_mod.unescape(re.sub(r'<[^>]+>', ' ', text))
    clean = html_mod.unescape(re.sub(r'\s+', ' ', clean).strip())
    for pat in SALARY_RE:
        m = pat.search(clean)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                if "k" in m.group(0).lower():
                    vmin = int(float(raw_min) * 1000)
                    vmax = int(float(raw_max) * 1000)
                else:
                    vmin = int(float(raw_min))
                    vmax = int(float(raw_max))
                if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
                    return vmin, vmax
            except (ValueError, IndexError):
                continue
    return None


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== WA Lever scraper started ===")
    log(f"Output: {OUTPUT_FILE}")

    log(f"Running Exa discovery ({len(DISCOVERY_QUERIES)} queries)...")
    extra_slugs = discover_slugs(SEED_SLUGS)
    log(f"  {len(SEED_SLUGS)} seed + {len(extra_slugs)} discovered = "
        f"{len(SEED_SLUGS) + len(extra_slugs)} total slugs")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    total_found = 0
    api_failures = 0
    discovered_slug_yield = {}
    all_slugs = list(SEED_SLUGS) + sorted(extra_slugs)

    for slug in all_slugs:
        jobs = fetch_company_jobs(slug)
        if not jobs:
            api_failures += 1
            time.sleep(1)
            continue

        company_name = slug.replace("-", " ").replace("_", " ").replace(".", " ").title()
        log(f"\n── {company_name} ({slug}): {len(jobs)} postings ──")
        wa_count = 0
        found_this = 0

        for job in jobs:
            cats = job.get("categories") or {}
            loc_name = cats.get("location", "") or cats.get("allLocations", "")
            if isinstance(loc_name, list):
                loc_name = ", ".join(loc_name)

            desc_plain = job.get("descriptionPlain") or ""
            if not is_washington(loc_name, desc_plain):
                continue
            wa_count += 1

            title = (job.get("text") or "").strip()
            if not title:
                continue

            key = f"{title.lower()}|{company_name.lower()}"
            if key in seen_keys:
                continue

            salary = extract_salary_from_range(job.get("salaryRange"))
            if not salary:
                sal_desc = job.get("salaryDescriptionPlain") or job.get("salaryDescription") or ""
                salary = extract_salary_from_text(sal_desc) or extract_salary_from_text(desc_plain)

            if not salary:
                log(f"  [{title[:50]}] → no salary")
                continue

            vmin, vmax = salary
            job_id = job.get("id", "")
            abs_url = f"https://jobs.lever.co/{slug}/{job_id}" if job_id else ""

            posted = TODAY
            created_ms = job.get("createdAt")
            if created_ms:
                try:
                    posted = datetime.fromtimestamp(
                        int(created_ms) / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    pass

            job_out = {
                "role":            title,
                "company":         company_name,
                "min":             vmin,
                "max":             vmax,
                "location":        parse_location(loc_name),
                "source_url":      abs_url,
                "posted":          posted,
                "source_platform": "lever",
            }

            write_job(OUTPUT_FILE, job_out)
            seen_keys.add(key)
            total_found += 1
            found_this += 1
            log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{loc_name}]")

        log(f"  WA: {wa_count} | New w/ salary: {found_this}")
        if slug in extra_slugs:
            discovered_slug_yield[slug] = found_this
        time.sleep(2)

    log(f"\n=== WA Lever scraper complete: {total_found} new jobs (api_failures={api_failures}) ===")

    # Auto-inject high-yield discovered slugs into SEED_SLUGS
    seed_set = set(SEED_SLUGS)
    newly_qualified = {
        slug: count for slug, count in discovered_slug_yield.items()
        if slug not in seed_set and count >= 2
    }
    if newly_qualified:
        log(f"\nAuto-injecting {len(newly_qualified)} high-yield slug(s) into SEED_SLUGS:")
        script_path = os.path.abspath(__file__)
        try:
            source = open(script_path).read()
            new_lines = []
            for slug, count in sorted(newly_qualified.items(), key=lambda x: -x[1]):
                if f'"{slug}"' in source:
                    continue
                log(f"  + {slug} ({count} WA+salary jobs)")
                new_lines.append(f'    "{slug}",  # auto-discovered {TODAY} — {count} WA+salary')
            if new_lines:
                marker = "]\n\nDISCOVERY_QUERIES"
                source = source.replace(marker, "\n" + "\n".join(new_lines) + "\n" + marker)
                open(script_path, "w").write(source)
        except Exception as e:
            log(f"  Auto-inject error: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
