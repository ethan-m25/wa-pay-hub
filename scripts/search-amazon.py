#!/usr/bin/env python3
"""
wa-pay-hub/scripts/search-amazon.py
Amazon Jobs scraper — WA edition.

Strategy: Query amazon.jobs/en/search.json for WA locations.
Salary is in the job page HTML, not the API response — fetched per job.
"""
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

from scrapling import Fetcher

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE,
)

LOG_FILE  = os.path.expanduser("~/wa-pay-hub/scripts/amazon.log")
LOCK_FILE = os.path.expanduser("~/wa-pay-hub/scripts/.amazon.lock")

log = make_logger(LOG_FILE)
fetcher = Fetcher()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

BASE_URL     = "https://amazon.jobs/en/search.json"
RESULT_LIMIT = 100
COUNTRY      = "USA"

QUERY_LOCATIONS = ['Seattle, Washington, USA', 'Bellevue, Washington, USA', 'Redmond, Washington, USA', 'Kirkland, Washington, USA', 'Virtual Location - Washington, USA']

REGION_TERMS = ['washington', 'seattle', 'bellevue', 'redmond', 'kirkland', 'tacoma', 'spokane', 'olympia', ', wa']

CITY_MAP = {'seattle': 'Seattle, WA', 'bellevue': 'Bellevue, WA', 'redmond': 'Redmond, WA', 'kirkland': 'Kirkland, WA', 'tacoma': 'Tacoma, WA'}

SALARY_RE = [
    re.compile(
        r'\$\s*([\d,]+(?:\.\d+)?)\s*[-–—]\s*\$\s*([\d,]+(?:\.\d+)?)\s*(?:USD|per year|annually|annual)?',
        re.IGNORECASE,
    ),
    re.compile(
        r'(?:pay|salary|compensation)[^$\n]{0,60}([\d,]{5,})(?:\.\d+)?\s*[-–—to]+\s*([\d,]{5,})',
        re.IGNORECASE,
    ),
]


def _api_fetch(location, offset):
    params = urllib.parse.urlencode([
        ("normalized_location[]", location),
        ("result_limit", RESULT_LIMIT),
        ("offset", offset),
        ("country[]", COUNTRY),
    ])
    req = urllib.request.Request(f"{BASE_URL}?{params}")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, */*; q=0.01")
    req.add_header("Accept-Language", "en,en-US;q=0.9")
    req.add_header("Referer", "https://www.amazon.jobs/en/search")
    req.add_header("X-Requested-With", "XMLHttpRequest")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"  API error ({location} offset={offset}): {e}")
        return None


def fetch_region_jobs():
    all_jobs = []
    seen_ids = set()
    for location in QUERY_LOCATIONS:
        offset = 0
        while True:
            data = _api_fetch(location, offset)
            if not data:
                break
            jobs = data.get("jobs", [])
            hits = data.get("hits", 0)
            log(f"  [{location}] offset={offset}: {len(jobs)}/{hits}")
            if not jobs:
                break
            for job in jobs:
                job_id = str(job.get("id_icims") or job.get("id", ""))
                if job_id and job_id not in seen_ids:
                    seen_ids.add(job_id)
                    all_jobs.append(job)
            offset += RESULT_LIMIT
            if offset >= hits or len(jobs) < RESULT_LIMIT:
                break
            time.sleep(1)
        time.sleep(2)
    return all_jobs


def is_region_job(location_str):
    loc = (location_str or "").lower()
    return any(t in loc for t in REGION_TERMS)


def parse_location(location_str):
    loc = (location_str or "").lower()
    for city, label in CITY_MAP.items():
        if city in loc:
            return label
    return "WA, WA"


def fetch_page_salary(job_path):
    url = f"https://www.amazon.jobs{job_path}"
    try:
        page = fetcher.get(url, timeout=20)
        if page.status != 200:
            return None
        html = page.body
    except Exception as e:
        log(f"  Page fetch error ({job_path}): {e}")
        return None
    html_str = html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else html
    text = html_mod.unescape(re.sub(r'<[^>]+>', ' ', html_str))
    text = html_mod.unescape(re.sub(r'\s+', ' ', text).strip())
    return _extract_salary(text)


def _extract_salary(text):
    for pat in SALARY_RE:
        m = pat.search(text)
        if not m:
            continue
        try:
            vmin = int(m.group(1).replace(",", "").split(".")[0])
            vmax = int(m.group(2).replace(",", "").split(".")[0])
            if 15_000 <= vmin <= 1_500_000 and vmin < vmax:
                return vmin, vmax
        except (ValueError, IndexError):
            continue
    return None


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== Amazon Jobs WA scraper started ===")
    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    log("Fetching jobs from amazon.jobs API...")
    all_jobs = fetch_region_jobs()
    log(f"Unique jobs fetched: {len(all_jobs)}")

    total_found = 0
    for job in all_jobs:
        location_str = job.get("location", "")
        if not is_region_job(location_str):
            continue
        title = (job.get("title") or "").strip()
        if not title:
            continue
        key = f"{title.lower()}|amazon"
        if key in seen_keys:
            continue
        job_path = job.get("job_path", "")
        if not job_path:
            continue
        salary = fetch_page_salary(job_path)
        if not salary:
            time.sleep(0.5)
            continue
        vmin, vmax = salary
        abs_url = f"https://www.amazon.jobs{job_path}"
        posted = TODAY
        date_m = re.search(r'(\d{4}-\d{2}-\d{2})', job.get("posted_date") or "")
        if date_m:
            posted = date_m.group(1)
        write_job(OUTPUT_FILE, {
            "role":            title,
            "company":         "Amazon",
            "min":             vmin,
            "max":             vmax,
            "location":        parse_location(location_str),
            "source_url":      abs_url,
            "posted":          posted,
            "source_platform": "amazon",
        })
        seen_keys.add(key)
        total_found += 1
        log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{location_str}]")
        time.sleep(1)

    log(f"=== Amazon WA scraper complete: {total_found} new jobs ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
