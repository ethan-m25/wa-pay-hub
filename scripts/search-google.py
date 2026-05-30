#!/usr/bin/env python3
# wa-pay-hub/scripts/search-google.py
# Google Careers scraper for WA. USD salary: '$194,000 - $199,000 USD' or 'USD 194,000 – 199,000'

LOG_FILE  = "/Users/clawii/wa-pay-hub/scripts/google.log"
LOCK_FILE = "/Users/clawii/wa-pay-hub/scripts/.google.lock"

REGION_NAME      = 'WA'
SEARCH_LOCATIONS = [
    ("Seattle Washington", "Seattle, WA"),
    ("Bellevue Washington", "Bellevue, WA"),
    ("Kirkland Washington", "Kirkland, WA"),
]
REGION_TERMS     = ['seattle', 'bellevue', 'redmond', 'kirkland', 'tacoma', 'spokane', 'washington state', ', wa,', ', wa ', 'puget sound']
NON_REGION_TERMS = ['washington, dc', 'district of columbia']
CITY_MAP         = {'seattle': 'Seattle, WA', 'bellevue': 'Bellevue, WA', 'redmond': 'Redmond, WA', 'kirkland': 'Kirkland, WA', 'tacoma': 'Tacoma, WA', 'spokane': 'Spokane, WA'}
DEFAULT_LOCATION = 'Washington, WA'

import html as html_mod
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright
from scrapling import Fetcher

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE,
)

log = make_logger(LOG_FILE)
fetcher = Fetcher()

BASE_URL = "https://www.google.com/about/careers/applications/jobs/results/"

SALARY_RE = [
    # "$194,000 - $199,000 USD" or "$194,000 – $199,000"
    re.compile(r'\$\s*([\d,]+)\s*[-–—]\s*\$\s*([\d,]+)\s*(?:USD)?', re.IGNORECASE),
    # "USD 194,000 – 199,000"
    re.compile(r'USD\s*([\d,]+)\s*[-–—]\s*([\d,]+)', re.IGNORECASE),
    # "194,000 to 199,000 USD"
    re.compile(r'([\d,]{6,})\s+to\s+([\d,]{6,})\s*USD', re.IGNORECASE),
    # "$194K – $199K"
    re.compile(r'\$([\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*\$([\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
]


def _extract_salary(text):
    for pat in SALARY_RE:
        m = pat.search(text)
        if m:
            try:
                vmin = int(m.group(1).replace(",", ""))
                vmax = int(m.group(2).replace(",", ""))
                if "k" in m.group(0).lower():
                    vmin *= 1000
                    vmax *= 1000
                if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
                    return vmin, vmax
            except (ValueError, IndexError):
                continue
    return None


def _extract_text(html_bytes):
    html_str = html_bytes.decode("utf-8", errors="ignore") if isinstance(html_bytes, bytes) else html_bytes
    text = html_mod.unescape(re.sub(r'<[^>]+>', ' ', html_str))
    return re.sub(r'\s+', ' ', text).strip()


def _is_region(location_str):
    loc = (location_str or "").lower()
    if any(p in loc for p in NON_REGION_TERMS):
        return False
    return any(t in loc for t in REGION_TERMS)


def _parse_location(location_str):
    loc = (location_str or "").lower()
    for city, label in CITY_MAP.items():
        if city in loc:
            return label
    return DEFAULT_LOCATION


def _get_job_urls_via_playwright(location_query):
    search_url = f"{BASE_URL}?location={location_query.replace(' ', '+')}&employment_type=FULL_TIME"
    job_urls = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)

            try:
                page.locator("text=Agree").first.click(timeout=4_000)
                time.sleep(1)
            except Exception:
                pass

            page.wait_for_load_state("networkidle", timeout=20_000)
            time.sleep(2)

            page_num = 1
            while True:
                new_urls = page.evaluate(r"""() => {
                    const links = new Set();
                    document.querySelectorAll("a").forEach(a => {
                        if (a.href && a.href.includes("/jobs/results/") && /\d{10,}/.test(a.href)) {
                            links.add(a.href.split("?")[0]);
                        }
                    });
                    return Array.from(links);
                }""")

                job_urls.update(new_urls)
                log(f"  Page {page_num}: {len(new_urls)} URLs ({len(job_urls)} total)")

                next_link = page.get_by_label("Go to next page")
                if not next_link.is_visible(timeout=2_000):
                    break

                next_link.click()
                page.wait_for_load_state("networkidle", timeout=15_000)
                time.sleep(1.5)
                page_num += 1

        except Exception as e:
            log(f"  Playwright error: {e}")
        finally:
            browser.close()

    return job_urls


def _fetch_job_details(url):
    try:
        page = fetcher.get(url, timeout=20)
        if page.status != 200:
            return None
        body = page.body
        text = _extract_text(body)
    except Exception as e:
        log(f"  fetch error ({url[-40:]}): {e}")
        return None

    body_str = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else body
    title_m = re.search(r'<title[^>]*>([^<]+)</title>', body_str)
    title = title_m.group(1).strip() if title_m else ""
    title = re.sub(r'\s*[\|—]\s*(Google Careers|Google|YouTube).*$', '', title, flags=re.IGNORECASE).strip()
    if not title:
        slug_m = re.search(r'/results/\d+[-]([^?#]+)', url)
        if slug_m:
            title = slug_m.group(1).replace("-", " ").title()

    # Extract location from page text
    loc_str = ""
    for term in REGION_TERMS:
        if term in text.lower():
            loc_str = term
            break

    salary = _extract_salary(text)
    return title, loc_str, salary


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log(f"=== Google Careers scraper started ({REGION_NAME}) ===")
    log(f"Output: {OUTPUT_FILE}")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    all_job_urls = set()

    for location_query, default_loc in SEARCH_LOCATIONS:
        log(f"\nSearching: {location_query}")
        urls = _get_job_urls_via_playwright(location_query)
        all_job_urls.update(urls)
        log(f"  -> {len(urls)} URLs found")
        time.sleep(2)

    log(f"\n{len(all_job_urls)} unique job URLs collected")

    total_found = 0

    for url in sorted(all_job_urls):
        result = _fetch_job_details(url)
        if not result:
            time.sleep(0.5)
            continue

        title, location_str, salary = result

        if not title:
            continue

        if not _is_region(location_str):
            log(f"  [{title[:40]}] -> not {REGION_NAME} ({location_str})")
            time.sleep(0.5)
            continue

        if not salary:
            log(f"  [{title[:40]}] -> no salary")
            time.sleep(0.5)
            continue

        key = f"{title.lower()}|google"
        if key in seen_keys:
            time.sleep(0.3)
            continue

        vmin, vmax = salary

        job_out = {
            "role":            title,
            "company":         "Google",
            "min":             vmin,
            "max":             vmax,
            "location":        _parse_location(location_str),
            "source_url":      url,
            "posted":          TODAY,
            "source_platform": "google",
        }

        write_job(OUTPUT_FILE, job_out)
        seen_keys.add(key)
        total_found += 1
        log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{_parse_location(location_str)}]")
        time.sleep(1)

    log(f"\n=== Google Careers scraper complete: {total_found} new jobs written ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
