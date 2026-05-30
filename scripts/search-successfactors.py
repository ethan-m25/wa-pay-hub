#!/usr/bin/env python3
# wa-pay-hub/scripts/search-successfactors.py
# WA RCW 49.58.110 — salary range required in all postings (15+ employees, eff. Jan 1 2023)

LOG_FILE  = "/Users/clawii/wa-pay-hub/scripts/successfactors.log"
LOCK_FILE = "/Users/clawii/wa-pay-hub/scripts/.successfactors.lock"

REGION_NAME     = 'WA'
# === Phase 4 seed loader (added 2026-05-27) ===
sys.path.insert(0, os.path.expanduser('~/shared-scripts'))
from hub_employer_seeds import load_successfactors_seeds
SEED_PORTALS = load_successfactors_seeds('wa')
REGION_TERMS    = ['seattle', 'bellevue', 'redmond', 'kirkland', 'tacoma', 'spokane', 'olympia', 'bothell', 'renton', 'kent', 'everett', 'sammamish', 'issaquah', ', wa,', ', wa ', 'washington state', 'puget sound']
NON_REGION_TERMS = ['washington, dc', 'washington d.c', 'district of columbia']
CITY_MAP        = {'seattle': 'Seattle, WA', 'bellevue': 'Bellevue, WA', 'redmond': 'Redmond, WA', 'kirkland': 'Kirkland, WA', 'tacoma': 'Tacoma, WA', 'spokane': 'Spokane, WA', 'olympia': 'Olympia, WA', 'bothell': 'Bothell, WA', 'renton': 'Renton, WA', 'kent': 'Kent, WA', 'everett': 'Everett, WA'}
DEFAULT_LOCATION = 'Washington, WA'
SALARY_MIN      = 30000
SALARY_MAX      = 1000000

import html as html_mod
import os
import re
import sys
import time
from datetime import date, timedelta

from scrapling import Fetcher

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE,
)

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SALARY_RE = [
    # USD / CAD ($)
    re.compile(r'(?:USD\s*)?\$\s*([\d,]+)(?:\.\d+)?\s*(?:USD)?\s*[-–—to]+\s*(?:USD\s*)?\$\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'\$([\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*\$([\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
    re.compile(r'USD\s*([\d,]{5,})\s*[-–—to]+\s*([\d,]{5,})', re.IGNORECASE),
    re.compile(r'([\d,]{5,})\s+to\s+([\d,]{5,})\s*USD', re.IGNORECASE),
    # GBP (£)
    re.compile(r'£\s*([\d,]+)(?:\.\d+)?\s*[-–—to]+\s*£\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'£([\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*£([\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
    re.compile(r'GBP\s*([\d,]{5,})\s*[-–—to]+\s*([\d,]{5,})', re.IGNORECASE),
    # Generic fallback
    re.compile(r'(?:pay|salary|compensation|wage|salary range)[^$£\n]{0,50}([\d,]{5,})\s*[-–—to]+\s*[$£]?([\d,]{5,})', re.IGNORECASE),
]


def fetch_html(url):
    try:
        page = fetcher.get(url, timeout=25)
        if page.status != 200:
            return None
        return page.html_content or ""
    except Exception as e:
        log(f"  Fetch error ({url[:60]}): {e}")
        return None


def html_to_text(raw):
    raw = re.sub(r'<script[^>]*>.*?</script>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'<style[^>]*>.*?</style>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
    text = html_mod.unescape(re.sub(r'<[^>]+>', ' ', raw))
    return re.sub(r'\s+', ' ', text).strip()


def is_region(loc_text):
    loc = (loc_text or "").lower()
    if any(p in loc for p in NON_REGION_TERMS):
        return False
    return any(t in loc for t in REGION_TERMS)


def parse_location(loc_text):
    loc = (loc_text or "").lower()
    for city, label in CITY_MAP.items():
        if city in loc:
            return label
    return DEFAULT_LOCATION


def extract_salary(text):
    if not text:
        return None

    num_range = r'\$\s*([\d,]+(?:\.\d{1,2})?)\s*[-–—]\s*\$\s*([\d,]+(?:\.\d{1,2})?)'

    hourly_pat = re.compile(num_range + r'\s*(?:/|-per-|per\s+)?\s*hour(?:ly)?', re.IGNORECASE)
    m = hourly_pat.search(text)
    if m:
        try:
            vmin = int(float(m.group(1).replace(",", "")) * 2080)
            vmax = int(float(m.group(2).replace(",", "")) * 2080)
            if SALARY_MIN <= vmin <= SALARY_MAX and vmin < vmax:
                return vmin, vmax
        except (ValueError, IndexError):
            pass

    for pat in SALARY_RE:
        m = pat.search(text)
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
                if SALARY_MIN <= vmin <= SALARY_MAX and vmin < vmax:
                    return vmin, vmax
            except (ValueError, IndexError):
                continue
    return None


def extract_title(raw_html):
    if raw_html:
        m = re.search(r'<h1[^>]*>(.*?)</h1>', raw_html, re.DOTALL | re.IGNORECASE)
        if m:
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()
            if len(title) >= 5:
                return title
        m = re.search(r'<title[^>]*>([^<]+)</title>', raw_html, re.IGNORECASE)
        if m:
            title = html_mod.unescape(m.group(1)).strip()
            title = re.sub(r'\s*[\|—–-]\s*\w.*$', '', title).strip()
            if len(title) >= 5:
                return title
    return None


def extract_posted(text):
    m = re.search(r'(20\d{2}-\d{2}-\d{2})', (text or "")[:5000])
    if m:
        return m.group(1)
    return TODAY


def get_region_urls_from_sitemal(host):
    for xml_path in ("sitemal.xml", "sitemap.xml"):
        sitemal_url = f"https://{host}/{xml_path}"
        log(f"  Fetching {xml_path}: {sitemal_url}")
        raw = fetch_html(sitemal_url)
        if raw and "<item>" in raw:
            break
        raw = None

    if not raw:
        log(f"  -> sitemap fetch failed for {host}")
        return []

    items_raw = re.findall(r'<item>(.*?)</item>', raw, re.DOTALL)
    results = []
    for item_text in items_raw:
        link_m = re.search(r'<link>\s*(https?://[^\s<]+)', item_text)
        loc_m  = re.search(r'<g:location>(.*?)</g:location>', item_text)
        if not link_m:
            continue
        url = link_m.group(1).strip()
        loc = loc_m.group(1).strip() if loc_m else ""
        if is_region(loc):
            results.append((url, loc))

    log(f"  -> {len(results)} {REGION_NAME} jobs found in {host}")
    return results


def process_job(url, loc_text, company_name, seen_keys):
    raw_html = fetch_html(url)
    if not raw_html:
        return None

    text = html_to_text(raw_html)

    if not is_region(text) and not is_region(loc_text):
        return None

    title = extract_title(raw_html)
    if not title:
        return None

    key = f"{title.lower().strip()}|{company_name.lower().strip()}"
    if key in seen_keys:
        return None

    salary = extract_salary(text)
    if not salary:
        return None

    vmin, vmax = salary
    location = parse_location(loc_text or text)
    posted = extract_posted(text)

    return {
        "role":            title,
        "company":         company_name,
        "min":             vmin,
        "max":             vmax,
        "location":        location,
        "source_url":      url,
        "posted":          posted,
        "source_platform": "successfactors",
    }


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log(f"=== SuccessFactors scraper started ({REGION_NAME}) ===")
    log(f"Output: {OUTPUT_FILE}")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    total_found = 0
    no_salary = 0

    for host, company_name in SEED_PORTALS:
        log(f"\n--- {company_name} ({host}) ---")
        region_jobs = get_region_urls_from_sitemal(host)

        for url, loc_text in region_jobs:
            job = process_job(url, loc_text, company_name, seen_keys)
            if job is None:
                no_salary += 1
                time.sleep(0.5)
                continue

            write_job(OUTPUT_FILE, job)
            seen_keys.add(f"{job['role'].lower().strip()}|{company_name.lower().strip()}")
            total_found += 1
            log(f"  FOUND: {job['role'][:50]} | ${job['min']:,}–${job['max']:,} [{job['location']}]")
            time.sleep(1.5)

    log(
        f"\n=== SuccessFactors scraper complete: {total_found} new jobs written "
        f"(no_salary={no_salary}) ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
