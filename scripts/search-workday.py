#!/usr/bin/env python3
"""
wa-pay-hub/scripts/search-workday.py
Workday CXS API scraper — New York State edition.

WA RCW 49.58.110: employers with 15+ employees must post salary range + benefits.
Effective January 1, 2023. Seattle tech, retail, aerospace, and healthcare
companies are the main WA Workday users.

Strategy:
  1. Seed tenants (known NY Workday employers) + Exa discovery
  2. CXS JSON API — paginate all jobs per tenant, filter NY locations
  3. Fetch job HTML page — salary in <meta> / JSON-LD; regex extraction

Run: python3 ~/wa-pay-hub/scripts/search-workday.py
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, exa_search, load_existing_keys, load_existing_urls, write_job,
    TODAY, OUTPUT_FILE,
)

LOG_FILE      = os.path.expanduser("~/wa-pay-hub/scripts/workday.log")
LOCK_FILE     = os.path.expanduser("~/wa-pay-hub/scripts/.workday.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"
LARGE_TENANT_THRESHOLD = 500
REGION_SEARCH_TEXT = "Washington"

log = make_logger(LOG_FILE)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# === Phase 4 seed loader (added 2026-05-27) ===
sys.path.insert(0, os.path.expanduser('~/shared-scripts'))
from hub_employer_seeds import load_workday_seeds
SEED_TENANTS = load_workday_seeds('wa')

KNOWN_COMPANY_OVERRIDES = {
    "starbucks":    "Starbucks",
    "nordstrom":    "Nordstrom",
    "costco":       "Costco Wholesale",
    "alaskaair":    "Alaska Airlines",
    "t-mobile":     "T-Mobile",
    "expedia":      "Expedia Group",
    "zillow":       "Zillow Group",
    "tableau":      "Tableau (Salesforce)",
    "smartsheet":   "Smartsheet",
    "premera":      "Premera Blue Cross",
    "providence":   "Providence Health",
    "seagen":       "Seagen (Pfizer Oncology)",
    "boeing":       "Boeing",
    "uw":           "University of Washington",
    "adobe":        "Adobe",
    "salesforce":   "Salesforce",
    "pwc":          "PwC",
}

DISCOVERY_QUERIES = [
    'site:myworkdayjobs.com "Seattle" OR "Bellevue" salary 2026',
    'site:myworkdayjobs.com "Washington State" OR "Seattle, WA" job salary 2026',
    'site:myworkdayjobs.com "Redmond" OR "Kirkland" OR "Bothell" salary 2026',
    'site:myworkdayjobs.com Seattle tech OR software OR engineering salary 2026',
    'site:myworkdayjobs.com Seattle OR Bellevue healthcare OR biotech salary 2026',
    'site:myworkdayjobs.com "Washington" aerospace OR retail OR logistics salary 2026',
    'site:myworkdayjobs.com "Tacoma" OR "Spokane" OR "Renton" salary 2026',
]

WA_TERMS = [
    "washington", "seattle", "bellevue", "redmond", "kirkland", "bothell",
    "tacoma", "spokane", "renton", "kent", "everett", "federal way",
    "issaquah", "sammamish", "shoreline", "burien", ", wa,", "wa,",
    "washington state",
]
_WA_PATH_TERMS = ["-seattle", "-bellevue", "-washington", "-wa-", "/seattle/", "/washington/"]
_NON_WA_PATH_TERMS = [
    "/california/", "/san-francisco/", "/los-angeles/", "/new-york/", "/chicago/",
    "/boston/", "/texas/", "/florida/", "/atlanta/", "/denver/",
    "/toronto/", "/ontario/", "/british-columbia/", "/london-london/",
    "-usa-east", "ny-usa", "ca-usa", "tx-usa",
]

SALARY_RE = [
    re.compile(r'\$\s*([\d,]+)(?:\.\d+)?\s*(?:USD|usd)?\s*[-–—to]+\s*\$\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'\$([\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*\$([\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
    re.compile(r'(?:pay|salary|compensation|base|wage|range|annual)[^$\n]{0,60}\$?([\d,]{5,})\s*[-–—to]+\s*\$?([\d,]{5,})', re.IGNORECASE),
    re.compile(r'salary\s+range\s*:\s*([\d,]+)\s*[-–—]\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'between\s+\$\s*([\d,]+)(?:\.\d+)?\s+and\s+\$\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'\$\s*([\d,]+)(?:\.\d+)?\s+(?:and|to)\s+\$\s*([\d,]+)\s+per\s+year', re.IGNORECASE),
]

SITEMAP_SOURCES = [
    ("https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/siteMap.xml", "Boeing"),
]

_WD_URL_RE = re.compile(
    r'https?://([a-z0-9][a-z0-9-]*)\.wd\d+\.myworkdayjobs\.com(?:/[a-z]{2}-[A-Z]{2})?/([^/?#]+)',
    re.IGNORECASE,
)
_WD_SITE_URL_RE = re.compile(
    r'https?://wd\d+\.myworkdaysite\.com(?:/[a-z]{2}-[A-Z]{2})?/recruiting/([a-z0-9][a-z0-9-]*)/([^/?#]+)',
    re.IGNORECASE,
)
_SKIP_TENANTS = {'job', 'jobs', 'search', 'en', 'en-us', 'en-gb', 'fr', 'details', 'recruiting'}
_NUMERIC_PREFIX_RE = re.compile(r'^\d{3,5}\s+')

def format_tenant_name(company_id, tenant):
    override = KNOWN_COMPANY_OVERRIDES.get(company_id.lower())
    if override:
        return override
    clean = re.sub(r'(?i)(External|Careers?|Jobs?|_[A-Z]{2}$)', '', tenant)
    clean = clean.replace('_', ' ').strip()
    words = re.sub(r'([a-z])([A-Z])', r'\1 \2', clean).split()
    if len(words) >= 2:
        return ' '.join(words)
    return company_id.replace('-', ' ').title()

def parse_workday_tenant(url):
    m = _WD_URL_RE.match(url)
    if not m:
        return None
    company_id = m.group(1).lower()
    host_m = re.match(r'https?://([^/]+)', url)
    if not host_m:
        return None
    host = host_m.group(1).lower()
    tenant = m.group(2)
    if tenant.lower() in _SKIP_TENANTS or len(tenant) < 3:
        return None
    return host, company_id, tenant

def discover_tenants():
    discovered = {}
    candidate_urls = {}
    for i, query in enumerate(DISCOVERY_QUERIES, 1):
        log(f"  Discovery Exa [{i}/{len(DISCOVERY_QUERIES)}]: {query[:60]}...")
        resp = exa_search(query, num_results=15, start_date=LOOKBACK_DATE, log=log)
        if not resp:
            continue
        results = resp.get("results", [])
        new = 0
        for r in results:
            url = (r.get("url") or "").strip()
            parsed = parse_workday_tenant(url)
            if parsed and parsed[0] not in discovered:
                host, company_id, tenant = parsed
                discovered[host] = (host, company_id, tenant, format_tenant_name(company_id, tenant))
                new += 1
            job_url = parse_workday_job_url(url)
            if job_url:
                host, company_id, tenant, external_path = job_url
                candidate_urls[url] = {
                    "host": host, "company_id": company_id, "tenant": tenant,
                    "external_path": external_path,
                    "fallback_company": format_tenant_name(company_id, tenant),
                }
        log(f"    → {len(results)} results, {new} new tenants")
        time.sleep(1.5)
    return list(discovered.values()), candidate_urls

def wd_list_jobs(host, company_id, tenant, offset=0, limit=10, search_text=""):
    url = f"https://{host}/wday/cxs/{company_id}/{tenant}/jobs"
    body = json.dumps({"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search_text})
    cmd = [
        "curl", "-s", "--max-time", "20",
        "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "-H", f"User-Agent: {UA}",
        "-d", body,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=25)
        if result.returncode != 0:
            return [], 0
        data = json.loads(result.stdout)
        if "total" not in data:
            return [], 0
        return data.get("jobPostings", []), data.get("total", 0)
    except Exception as e:
        log(f"  API error ({host}): {e}")
        return [], 0

def is_washington(locations_text, external_path=""):
    ep = (external_path or "").lower()
    lt = (locations_text or "").lower()
    if any(t in ep for t in _NON_WA_PATH_TERMS):
        return False
    return any(t in lt for t in WA_TERMS) or any(t in ep for t in _WA_PATH_TERMS)

def parse_location(locations_text, external_path=""):
    lt = (locations_text or "").lower()
    city_map = {
        "seattle": "Seattle, WA", "bellevue": "Bellevue, WA",
        "redmond": "Redmond, WA", "kirkland": "Kirkland, WA",
        "bothell": "Bothell, WA", "tacoma": "Tacoma, WA",
        "spokane": "Spokane, WA", "renton": "Renton, WA",
        "issaquah": "Issaquah, WA", "kent": "Kent, WA",
        "everett": "Everett, WA",
    }
    for city, label in city_map.items():
        if city in lt:
            return label
    if "washington" in lt:
        return "Seattle, WA"
    return "Seattle, WA"

def fetch_job_html(host, tenant, external_path, company_id=""):
    if "myworkdaysite.com" in host:
        url = f"https://{host}/en-US/recruiting/{company_id}/{tenant}{external_path}"
    else:
        url = f"https://{host}/en-US/{tenant}{external_path}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.9")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    try:
        with urllib.request.urlopen(req, timeout=18) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

def fetch_job_html_from_url(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.9")
    try:
        with urllib.request.urlopen(req, timeout=18) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

def parse_workday_job_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    netloc = (parsed.netloc or "").lower()
    if "myworkdayjobs.com" not in netloc:
        return None
    host = netloc
    company_id = host.split(".")[0]
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return None
    tenant_idx = 1 if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]) else 0
    if len(parts) <= tenant_idx + 1:
        return None
    tenant = parts[tenant_idx]
    if parts[tenant_idx + 1].lower() != "job":
        return None
    external_path = "/" + "/".join(parts[tenant_idx + 1:])
    return host, company_id, tenant, external_path

def normalize_company_name(name):
    if not name:
        return name
    name = _NUMERIC_PREFIX_RE.sub('', name).strip()
    import html as _html
    return _html.unescape(name)

def extract_company_from_html(text):
    if not text:
        return None
    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.DOTALL | re.IGNORECASE
    )
    for block in ld_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list):
                data = data[0]
            org = data.get("hiringOrganization", {})
            name = org.get("name", "").strip()
            if name and len(name) > 1 and not re.match(r'^Company\s+\d+\b', name):
                return normalize_company_name(name)
        except Exception:
            continue
    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
                  text, re.IGNORECASE)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']',
                      text, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        if name and name.lower() not in ('workday', 'myworkdayjobs.com'):
            return normalize_company_name(name)
    return None

def extract_title_from_html(text):
    if not text:
        return None
    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.DOTALL | re.IGNORECASE
    )
    for block in ld_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list):
                data = data[0]
            name = (data.get("title") or data.get("name") or "").strip()
            if name:
                return name
        except Exception:
            continue
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<title>(.*?)</title>',
    ):
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r'\s+', ' ', m.group(1)).strip()
            title = re.sub(r'\s*[-|]\s*Workday.*$', '', title, flags=re.IGNORECASE)
            if title:
                return title
    return None

def extract_posted_from_html(text):
    if not text:
        return TODAY
    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.DOTALL | re.IGNORECASE
    )
    for block in ld_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list):
                data = data[0]
            posted = str(data.get("datePosted") or "").strip()
            m = re.search(r'(\d{4}-\d{2}-\d{2})', posted)
            if m:
                return m.group(1)
        except Exception:
            continue
    return TODAY

def extract_salary(text):
    if not text:
        return None
    for pattern in SALARY_RE:
        m = pattern.search(text)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                if "k" in m.group(0).lower():
                    val_min = int(float(raw_min) * 1000)
                    val_max = int(float(raw_max) * 1000)
                else:
                    val_min = int(float(raw_min))
                    val_max = int(float(raw_max))
                if 30_000 <= val_min <= 2_000_000 and val_min < val_max:
                    return val_min, val_max
            except (ValueError, IndexError):
                continue
    return None

def fetch_sitemap_jobs(sitemap_url):
    """Fetch job URLs from a Workday siteMap.xml (no auth required)."""
    import xml.etree.ElementTree as ET
    cmd = ["curl", "-s", "--max-time", "20", "-H", "Accept: application/xml",
           "-A", UA, sitemap_url]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=25)
        if result.returncode != 0 or not result.stdout:
            return []
        root = ET.fromstring(result.stdout)
    except Exception:
        return []
    ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text for loc in root.findall(".//ns:loc", ns) if loc.text]

def extract_location_from_html(text, external_path=""):
    if not text:
        return parse_location("", external_path)
    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.DOTALL | re.IGNORECASE
    )
    for block in ld_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list):
                data = data[0]
            loc = data.get("jobLocation")
            if isinstance(loc, list):
                loc = loc[0]
            addr = (loc or {}).get("address", {})
            locality = (addr.get("addressLocality") or "").strip()
            if locality:
                return locality
        except Exception:
            continue
    return parse_location("", external_path)

def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== WA Workday scraper started ===")
    log(f"Output: {OUTPUT_FILE}")

    log(f"Seed tenants: {len(SEED_TENANTS)} | Running Exa discovery...")
    discovered, candidate_urls = discover_tenants()

    seed_hosts = {t[0] for t in SEED_TENANTS}
    extra = [t for t in discovered if t[0] not in seed_hosts]
    all_tenants = SEED_TENANTS + extra
    log(f"Total tenants: {len(all_tenants)} ({len(SEED_TENANTS)} seed + {len(extra)} discovered)")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    seen_urls = load_existing_urls()
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    total_found = 0
    api_failures = 0
    failed_hosts = set()

    for host, company_id, tenant, company_name in all_tenants:
        log(f"\n── {company_name} ({host}) ──")
        wa_jobs = []
        offset = 0
        limit = 10
        max_pages = 10
        known_total = 0

        while offset // limit < max_pages:
            postings, total = wd_list_jobs(host, company_id, tenant, offset, limit)
            if not postings:
                if offset == 0:
                    api_failures += 1
                    failed_hosts.add(host)
                break
            if total > 0:
                known_total = total
            if offset == 0 and known_total > LARGE_TENANT_THRESHOLD and not use_search_text:
                use_search_text = REGION_SEARCH_TEXT
                max_pages = 9999
                log(f"  Large tenant ({known_total} jobs) → retrying with searchText='{use_search_text}'")
                postings, total = wd_list_jobs(host, company_id, tenant, 0, limit, use_search_text)
                if not postings:
                    break
                if total > 0:
                    known_total = total
            log(f"  API offset={offset}: {len(postings)} postings (total={total})")
            for p in postings:
                if is_washington(p.get("locationsText", ""), p.get("externalPath", "")):
                    wa_jobs.append(p)
            offset += limit
            if known_total > 0 and offset >= known_total:
                break
            time.sleep(2)

        log(f"  WA jobs: {len(wa_jobs)}")

        for i, posting in enumerate(wa_jobs, 1):
            title    = posting.get("title", "").strip()
            ext_path = posting.get("externalPath", "")
            posted_on = posting.get("postedOn", TODAY)
            locations = posting.get("locationsText", "")

            key = f"{title.lower()}|{company_name.lower()}"
            if key in seen_keys:
                continue

            log(f"  [{i}/{len(wa_jobs)}] {title[:55]}")
            text = fetch_job_html(host, tenant, ext_path, company_id=company_id)
            if not text:
                log("    → fetch failed")
                time.sleep(0.5)
                continue

            salary = extract_salary(text)
            if not salary:
                log("    → no salary")
                time.sleep(0.3)
                continue

            val_min, val_max = salary
            location = parse_location(locations, ext_path)
            source_url = f"https://{host}/en-US/{tenant}{ext_path}"

            if source_url in seen_urls:
                continue
            resolved_company = extract_company_from_html(text) or company_name

            posted = TODAY
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', posted_on or "")
            if date_match:
                posted = date_match.group(1)

            job = {
                "role":            title,
                "company":         resolved_company,
                "min":             val_min,
                "max":             val_max,
                "location":        location,
                "source_url":      source_url,
                "posted":          posted,
                "source_platform": "workday",
            }

            seen_keys.add(key)
            seen_urls.add(source_url)
            write_job(OUTPUT_FILE, job)
            total_found += 1
            log(f"    → FOUND: ${val_min:,}–${val_max:,} [{location}]")
            time.sleep(0.8)

        time.sleep(60)

    direct_fallback_found = 0
    fallback_candidates = [
        (url, meta)
        for url, meta in candidate_urls.items()
        if meta["host"] in failed_hosts
    ][:30]
    if fallback_candidates:
        log(
            f"\n── Direct Workday URL fallback ({len(fallback_candidates)} candidates "
            f"from {len(failed_hosts)} failed hosts) ──"
        )
    for index, (url, meta) in enumerate(fallback_candidates, 1):
        host = meta["host"]
        company_id = meta["company_id"]
        tenant = meta["tenant"]
        external_path = meta["external_path"]
        fallback_company = meta["fallback_company"]
        log(f"  [fallback {index}/{len(fallback_candidates)}] {host}")
        html = fetch_job_html_from_url(url)
        if not html:
            continue
        salary = extract_salary(html)
        if not salary:
            continue
        title = extract_title_from_html(html)
        if not title:
            continue
        company_name = extract_company_from_html(html) or fallback_company or format_tenant_name(company_id, tenant)
        key = f"{title.lower()}|{company_name.lower()}"
        if key in seen_keys:
            continue
        location = extract_location_from_html(html, external_path)
        if not is_washington(location, external_path):
            continue
        val_min, val_max = salary
        job = {
            "role": title,
            "company": company_name,
            "min": val_min,
            "max": val_max,
            "location": location,
            "source_url": url,
            "posted": extract_posted_from_html(html),
            "source_platform": "workday",
        }
        write_job(OUTPUT_FILE, job)
        seen_keys.add(key)
        total_found += 1
        direct_fallback_found += 1
        log(f"  → DIRECT FOUND: {title[:50]} @ {company_name} ${val_min:,}–${val_max:,} [{location}]")
        time.sleep(0.4)

    sitemap_found = 0
    if SITEMAP_SOURCES:
        log(f"\n── siteMap.xml fallback for 422-blocked companies ({len(SITEMAP_SOURCES)} sources) ──")
    for sitemap_url, sitemap_company in SITEMAP_SOURCES:
        log(f"  Fetching siteMap: {sitemap_company}")
        job_urls = fetch_sitemap_jobs(sitemap_url)
        if not job_urls:
            log(f"    → no URLs returned")
            continue
        wa_urls = [u for u in job_urls if any(t in u.lower() for t in
                   ["seattle", "renton", "bellevue", "redmond", "kirkland",
                    "tukwila", "-wa-", "washington-united-states"])]
        if not wa_urls:
            wa_urls = job_urls
        log(f"    → {len(job_urls)} total, {len(wa_urls)} plausibly WA — checking each")
        for job_url in wa_urls[:50]:
            html = fetch_job_html_from_url(job_url)
            if not html:
                continue
            salary = extract_salary(html)
            if not salary:
                continue
            title = extract_title_from_html(html)
            if not title:
                continue
            company_name = extract_company_from_html(html) or sitemap_company
            key = f"{title.lower()}|{company_name.lower()}"
            if key in seen_keys:
                continue
            ext_path = "/" + job_url.split("/job/", 1)[-1] if "/job/" in job_url else ""
            location = extract_location_from_html(html, ext_path)
            if not is_washington(location, ext_path):
                continue
            val_min, val_max = salary
            job = {
                "role": title,
                "company": company_name,
                "min": val_min,
                "max": val_max,
                "location": location,
                "source_url": job_url,
                "posted": extract_posted_from_html(html),
                "source_platform": "workday",
            }
            write_job(OUTPUT_FILE, job)
            seen_keys.add(key)
            total_found += 1
            sitemap_found += 1
            log(f"    → SITEMAP FOUND: {title[:50]} @ {company_name} ${val_min:,}–${val_max:,} [{location}]")
            time.sleep(0.4)
        log(f"    → {sitemap_company}: done")

    log(
        f"\n=== WA Workday scraper complete: {total_found} new jobs "
        f"(api_failures={api_failures}, direct_fallback={direct_fallback_found}, sitemap={sitemap_found}) ==="
    )
    return 0

if __name__ == "__main__":
    sys.exit(main())
