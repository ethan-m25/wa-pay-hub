#!/usr/bin/env python3
"""
wa-pay-hub/scripts/search-ashby.py
Ashby job board scraper — WA edition.

WA RCW 49.58.110, effective January 1, 2023.

Strategy:
  1. Fetch https://jobs.ashbyhq.com/{slug} — boards with embedded JSON (server-rendered)
  2. Parse jobPostings array; filter WA locations
  3. Salary from compensationTierSummary or individual job page fallback

Run: python3 ~/wa-pay-hub/scripts/search-ashby.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE,
)

LOG_FILE  = os.path.expanduser("~/wa-pay-hub/scripts/ashby.log")
LOCK_FILE = os.path.expanduser("~/wa-pay-hub/scripts/.ashby.lock")

log = make_logger(LOG_FILE)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"

SEED_SLUGS = [
    ("highspot", "Highspot"),
    ("outreach", "Outreach"),
    ("smartsheet", "Smartsheet"),
    ("convoy", "Convoy"),
    ("remitly", "Remitly"),
    ("rover", "Rover"),
    ("accolade", "Accolade"),
    ("seeq", "Seeq"),
    ("extrahop", "ExtraHop"),
]

STATE_TERMS = ["washington", "seattle", "bellevue", "redmond", "kirkland", "bothell", "tacoma", ", wa", "remote"]

SALARY_RE = [
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*[kK]?\s*[-–—]\s*\$\s*([\d,]+(?:\.\d+)?)\s*[kK]?', re.IGNORECASE),
    re.compile(r'([\d,]+)\s*[-–—]\s*([\d,]+)\s*(?:USD|per year|annually)', re.IGNORECASE),
]


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"  fetch error ({url}): {e}")
        return None


def _parse_salary(summary):
    if not summary:
        return None
    if "per hour" in summary.lower() or "/hr" in summary.lower():
        return None
    m = re.search(r'\$([\d,.]+)\s*[kK]?\s*[-–—]\s*\$([\d,.]+)\s*[kK]?', summary)
    if m:
        try:
            def parse_num(s, ctx):
                s = s.replace(",", "")
                v = float(s)
                if "k" in ctx.lower() and v < 1000:
                    v *= 1000
                return int(v)
            k = "k" in summary[m.start():m.end()].lower()
            vmin = parse_num(m.group(1), "k" if k else "")
            vmax = parse_num(m.group(2), "k" if k else "")
            if 30000 <= vmin <= 2000000 and vmin < vmax:
                return vmin, vmax
        except Exception:
            pass
    return None


def _parse_salary_text(text):
    if not text:
        return None
    for pat in SALARY_RE:
        m = pat.search(text)
        if not m:
            continue
        try:
            raw_min = m.group(1).replace(",", "")
            raw_max = m.group(2).replace(",", "")
            k = "k" in m.group(0).lower()
            vmin = int(float(raw_min) * (1000 if k and float(raw_min) < 1000 else 1))
            vmax = int(float(raw_max) * (1000 if k and float(raw_max) < 1000 else 1))
            if 30000 <= vmin <= 2000000 and vmin < vmax:
                return vmin, vmax
        except Exception:
            continue
    return None


def _is_state(location_str):
    loc = (location_str or "").lower()
    return any(t in loc for t in STATE_TERMS)


def _parse_location(location_str):
    loc = (location_str or "").lower()
    city_map = {
        "seattle": "Seattle, WA", "bellevue": "Bellevue, WA",
        "redmond": "Redmond, WA", "kirkland": "Kirkland, WA",
        "bothell": "Bothell, WA", "tacoma": "Tacoma, WA",
    }
    for k, v in city_map.items():
        if k in loc:
            return v
    if "remote" in loc:
        return "Remote (WA)"
    return "Seattle, WA"


def _parse_jobs_from_html(html):
    idx = html.find('jobPostings":[')
    if idx == -1:
        return None
    chunk = html[idx + len('jobPostings":['):]
    depth, i = 1, 0
    while i < len(chunk) and depth > 0:
        if chunk[i] == '[': depth += 1
        elif chunk[i] == ']': depth -= 1
        i += 1
    try:
        return json.loads('[' + chunk[:i])
    except json.JSONDecodeError:
        return None


def _fetch_job_salary(slug, job_id):
    url = f"https://jobs.ashbyhq.com/{slug}/{job_id}"
    html = _fetch(url)
    if not html:
        return None
    idx = html.find('"descriptionHtml"')
    if idx != -1:
        chunk = html[idx + len('"descriptionHtml"') + 1:]
        if chunk.startswith('"'):
            end = chunk.find('",\n') if '",\n' in chunk[:5000] else chunk.find('"', 1)
            desc_raw = chunk[1:end]
            desc_text = re.sub(r'<[^>]+>', ' ', html_mod.unescape(
                desc_raw.replace('\\n', '\n').replace('\\"', '"')))
            return _parse_salary_text(html_mod.unescape(re.sub(r'\s+', ' ', desc_text)))
    plain = html_mod.unescape(re.sub(r'<[^>]+>', ' ', html))
    return _parse_salary_text(re.sub(r'\s+', ' ', plain))


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== WA Ashby scraper started ===")
    log(f"Output: {OUTPUT_FILE}")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    total_found = 0
    skipped_spa = 0

    for slug, company_display in SEED_SLUGS:
        url = f"https://jobs.ashbyhq.com/{slug}"
        html = _fetch(url)
        if not html:
            log(f"── {slug}: fetch failed")
            continue

        jobs = _parse_jobs_from_html(html)
        if jobs is None:
            log(f"── {slug}: JS-rendered SPA — skipping")
            skipped_spa += 1
            time.sleep(1)
            continue

        log(f"\n── {company_display} ({slug}): {len(jobs)} jobs ──")
        state_count = 0
        found_this = 0

        for job in jobs:
            loc_name = job.get("locationName", "") or ""
            if not _is_state(loc_name):
                continue
            state_count += 1

            title = (job.get("title") or "").strip()
            if not title:
                continue

            key = f"{title.lower()}|{company_display.lower()}"
            if key in seen_keys:
                continue

            salary = _parse_salary(job.get("compensationTierSummary", ""))
            if not salary:
                job_id = job.get("id", "")
                if job_id:
                    salary = _fetch_job_salary(slug, job_id)
                    time.sleep(0.5)

            if not salary:
                log(f"  [{title[:50]}] → no salary")
                continue

            vmin, vmax = salary
            posted = TODAY
            date_m = re.search(r'(\d{4}-\d{2}-\d{2})', job.get("publishedDate") or "")
            if date_m:
                posted = date_m.group(1)

            job_out = {
                "role":            title,
                "company":         company_display,
                "min":             vmin,
                "max":             vmax,
                "location":        _parse_location(loc_name),
                "source_url":      f"https://jobs.ashbyhq.com/{slug}/{job.get('id', '')}",
                "posted":          posted,
                "source_platform": "ashby",
            }

            write_job(OUTPUT_FILE, job_out)
            seen_keys.add(key)
            total_found += 1
            found_this += 1
            log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{loc_name}]")

        log(f"  WA: {state_count} | New w/ salary: {found_this}")
        time.sleep(2)

    log(f"\n=== WA Ashby scraper complete: {total_found} new jobs (skipped_spa={skipped_spa}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
