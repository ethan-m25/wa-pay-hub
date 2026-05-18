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
    make_logger, acquire_lock, exa_search, load_existing_keys, write_job,
    TODAY, OUTPUT_FILE,
)

LOG_FILE      = os.path.expanduser("~/wa-pay-hub/scripts/workday.log")
LOCK_FILE     = os.path.expanduser("~/wa-pay-hub/scripts/.workday.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

SEED_TENANTS = [
    # Retail (Seattle/WA HQ)
    ("starbucks.wd5.myworkdayjobs.com",     "starbucks",    "Global",                  "Starbucks"),
    ("nordstrom.wd5.myworkdayjobs.com",     "nordstrom",    "Nordstrom_Careers",       "Nordstrom"),
    ("costco.wd5.myworkdayjobs.com",        "costco",       "CostcoExternalSite",      "Costco Wholesale"),
    ("rei.wd5.myworkdayjobs.com",           "rei",          "REICareers",              "REI"),
    ("weyerhaeuser.wd5.myworkdayjobs.com",  "weyerhaeuser", "WeyerhaeuserCareers",     "Weyerhaeuser"),
    # Airlines / transportation
    ("alaskaair.wd5.myworkdayjobs.com",     "alaskaair",    "AlaskaAirCareers",        "Alaska Airlines"),
    # Telecom / media
    ("t-mobile.wd1.myworkdayjobs.com",      "t-mobile",     "External",                "T-Mobile"),
    ("kcts9.wd1.myworkdayjobs.com",         "kcts9",        "KCTS9Careers",            "KCTS9"),
    # Tech (WA offices)
    ("salesforce.wd12.myworkdayjobs.com",   "salesforce",   "External_Career_Site",    "Salesforce"),
    ("tableau.wd1.myworkdayjobs.com",       "tableau",      "Tableau",                 "Tableau (Salesforce)"),
    ("expedia.wd5.myworkdayjobs.com",       "expedia",      "Expedia_Group_External",  "Expedia Group"),
    ("zillow.wd5.myworkdayjobs.com",        "zillow",       "Zillow_Group_External",   "Zillow Group"),
    ("f5.wd5.myworkdayjobs.com",            "f5",           "ExternalCareerSite",      "F5 Networks"),
    ("icertis.wd5.myworkdayjobs.com",       "icertis",      "Icertis",                 "Icertis"),
    ("smartsheet.wd5.myworkdayjobs.com",    "smartsheet",   "Smartsheet",              "Smartsheet"),
    # Healthcare / insurance
    ("premera.wd5.myworkdayjobs.com",       "premera",      "PremeraCareers",          "Premera Blue Cross"),
    ("providence.wd5.myworkdayjobs.com",    "providence",   "External",                "Providence Health"),
    ("multicare.wd5.myworkdayjobs.com",     "multicare",    "External_Career_Site",    "MultiCare Health"),
    # Biotech / pharma
    ("seagen.wd1.myworkdayjobs.com",        "seagen",       "SeagenCareers",           "Seagen"),
    ("immunomedics.wd5.myworkdayjobs.com",  "immunomedics", "Immunomedics",            "Immunomedics"),
    # Financial services
    ("wamu.wd5.myworkdayjobs.com",          "wamu",         "External",                "Washington Federal"),
    ("hsbcna.wd3.myworkdayjobs.com",        "hsbcna",       "hsbc_na",                 "HSBC North America"),
    # Professional services
    ("pwc.wd3.myworkdayjobs.com",           "pwc",          "Global_Experienced_Careers", "PwC"),
    ("accenture.wd3.myworkdayjobs.com",     "accenture",    "AccentureCareers",        "Accenture"),
    ("deloitte.wd1.myworkdayjobs.com",      "deloitte",     "ExternalCareers",         "Deloitte"),
    # Aerospace / defense (Renton/Everett HQ)
    ("boeing.wd1.myworkdayjobs.com",        "boeing",       "EXTERNAL_CAREERS",        "Boeing"),
    # Education / public sector
    ("uw.wd5.myworkdayjobs.com",            "uw",           "UWHires",                 "University of Washington"),
    # Software / cloud (Seattle offices)
    ("adobe.wd5.myworkdayjobs.com",         "adobe",        "external_experienced",    "Adobe"),
]


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
    "accenture":    "Accenture",
    "deloitte":     "Deloitte",
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


def wd_list_jobs(host, company_id, tenant, offset=0, limit=10):
    url = f"https://{host}/wday/cxs/{company_id}/{tenant}/jobs"
    body = json.dumps({"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""})
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
            write_job(OUTPUT_FILE, job)
            total_found += 1
            log(f"    → FOUND: ${val_min:,}–${val_max:,} [{location}]")
            time.sleep(0.8)

        time.sleep(60)

    log(
        f"\n=== WA Workday scraper complete: {total_found} new jobs "
        f"(api_failures={api_failures}) ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
