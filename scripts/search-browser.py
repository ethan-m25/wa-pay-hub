#!/usr/bin/env python3
"""
wa-pay-hub/scripts/search-browser.py
Browser-based scraper: Exa URL discovery + Playwright for JS-heavy portals.
Covers platforms not handled by dedicated search-greenhouse/lever/workday/ashby scripts.

Run: python3 ~/wa-pay-hub/scripts/search-browser.py
"""

import os
import sys
import time
from datetime import date, timedelta

from _common import (
    OUTPUT_FILE, TODAY, _UA,
    make_logger, acquire_lock,
    fetch_html_text, extract_job,
    load_existing_keys, collect_candidates, write_job,
    is_job_page,
)

LOG_FILE    = os.path.expanduser("~/wa-pay-hub/scripts/browser.log")
LOCK_FILE   = os.path.expanduser("~/wa-pay-hub/scripts/.browser.lock")
REGION_NAME = 'WA'

EXA_QUERIES = ['site:careers.microsoft.com "Washington" "salary" "$" 2026 engineer OR manager OR analyst', '"Microsoft" Seattle OR Redmond 2026 "salary range" "$" software engineer OR program manager', 'site:amazon.jobs Seattle OR Bellevue 2026 "salary range" "$" engineer OR manager', '"Boeing" Seattle OR Renton 2026 "salary range" "$" engineer OR analyst OR manager', 'site:tmobile.wd5.myworkdayjobs.com OR "careers.t-mobile.com" "Washington" 2026 "salary" "$"', '"Starbucks" OR "Costco" OR "Expedia" Seattle 2026 "salary range" "$" analyst OR manager', '"Tableau" OR "Salesforce" Seattle 2026 "salary range" "$" engineer OR manager OR analyst', '"Zillow" OR "Redfin" Seattle 2026 "salary range" "$" engineer OR analyst', 'site:careers.wa.gov "salary range" 2026 analyst OR manager OR specialist OR coordinator', 'Washington state employer 2026 "salary range" "$" site:jobs.lever.co OR site:job-boards.greenhouse.io', 'Seattle OR Bellevue 2026 "salary range" "$80,000" OR "$100,000" OR "$120,000" job -site:glassdoor.com -site:indeed.com']

log = make_logger(LOG_FILE)


# ── Playwright page fetch ─────────────────────────────────────────────────────
def _fetch_with_browser(url, timeout_ms=15000):
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        log("  Playwright not installed — pip3 install playwright && python3 -m playwright install chromium")
        return None, None

    attempts = [
        {"args": ["--disable-http2"], "wait_until": "domcontentloaded", "label": "browser-h1"},
        {"args": [], "wait_until": "commit", "label": "browser-commit"},
    ]

    last_err = None
    for idx, attempt in enumerate(attempts, 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=attempt["args"])
                ctx = browser.new_context(
                    user_agent=_UA,
                    locale="en-US",
                    viewport={"width": 1280, "height": 800},
                    ignore_https_errors=True,
                )
                page = ctx.new_page()
                page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf}", lambda r: r.abort())
                page.goto(url, wait_until=attempt["wait_until"], timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except PwTimeout:
                    pass
                text = page.inner_text("body")
                browser.close()
            if text:
                return text[:5000], attempt["label"]
        except Exception as e:
            last_err = e
            log(f"  Browser attempt {idx} failed ({attempt['label']}): {e}")
            time.sleep(1)

    if last_err:
        log(f"  Browser error: {last_err}")
    return None, None


def _fetch_page(url):
    text = fetch_html_text(url, max_chars=5000, skip_workday=False, min_content_len=300)
    if text:
        return text, "http"

    text, method = _fetch_with_browser(url)
    if text:
        return text, method

    retry_text = fetch_html_text(url, timeout=20, max_chars=5000, skip_workday=False, min_content_len=120)
    if retry_text:
        log("  HTTP fallback recovered partial content after browser failure")
        return retry_text, "http-retry"

    return None, method or "browser-failed"


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log(f"=== Browser scraper started ({REGION_NAME}) ===")
    log(f"Queries: {len(EXA_QUERIES)} | Output: {OUTPUT_FILE}")

    existing_keys = load_existing_keys()
    log(f"Existing jobs to skip: {len(existing_keys)}")

    lookback = (date.today() - timedelta(days=30)).isoformat() + "T00:00:00.000Z"
    candidates = collect_candidates(EXA_QUERIES, num_results=8, log=log, start_date=lookback)
    log(f"Unique URLs to process: {len(candidates)}")

    jobs_found = 0
    seen_keys = set(existing_keys)

    for i, (url, snippet) in enumerate(candidates.items(), 1):
        log(f"[{i:3d}/{len(candidates)}] {url[:70]}")
        t0 = time.time()

        page_text, method = _fetch_page(url)
        elapsed_fetch = time.time() - t0
        log(f"  fetch={method} {elapsed_fetch:.1f}s text={len(page_text) if page_text else 0}ch")

        if not page_text:
            log("  -> no content")
            time.sleep(1)
            continue

        if not is_job_page(page_text):
            log("  -> skip (not a job page)")
            continue

        t1 = time.time()
        try:
            job = extract_job(url, snippet, page_text, log)
        except Exception as e:
            log(f"  -> error: {e}")
            continue
        elapsed_llm = time.time() - t1

        if job:
            key = f"{job['role'].lower().strip()}|{job['company'].lower().strip()}"
            if key in seen_keys:
                log(f"  -> SKIP duplicate: {job['role']} @ {job['company']}")
                continue
            seen_keys.add(key)
            write_job(OUTPUT_FILE, job)
            jobs_found += 1
            log(f"  -> FOUND ({elapsed_llm:.1f}s): {job['role']} @ {job['company']} "
                f"${job['min']:,}–${job['max']:,} [{job.get('location', '')}]")
        else:
            log(f"  -> skip ({elapsed_llm:.1f}s)")

        time.sleep(0.5)

    log(f"=== Browser scraper complete: {jobs_found} new jobs written ===")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
