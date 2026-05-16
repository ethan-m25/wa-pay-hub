#!/usr/bin/env python3
"""
wa-pay-hub/scripts/update-jobs.py
Daily job data updater — NY Pay Hub edition.

Reads raw JSONL files from ~/.openclaw/shared/wa-jobs-raw-*.txt,
deduplicates, classifies, and writes to data/jobs.json.
"""

import glob
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime

TODAY = date.today().isoformat()
TIMESTAMP = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
REPO_DIR   = os.path.expanduser("~/wa-pay-hub")
DATA_FILE  = os.path.join(REPO_DIR, "data", "jobs.json")
SHARED_DIR = os.path.expanduser("~/.openclaw/shared")
RAW_PATTERN = os.path.join(SHARED_DIR, "wa-jobs-raw-*.txt")

CATEGORY_MAP = {
    "engineer": "Engineering",
    "developer": "Engineering",
    "software": "Engineering",
    "data": "Data & Analytics",
    "analyst": "Data & Analytics",
    "scientist": "Data & Analytics",
    "product": "Product",
    "design": "Design",
    "designer": "Design",
    "marketing": "Marketing",
    "finance": "Finance",
    "accounting": "Finance",
    "sales": "Sales",
    "operations": "Operations",
    "hr": "People & HR",
    "people": "People & HR",
    "recruiting": "People & HR",
    "legal": "Legal",
    "compliance": "Legal",
    "security": "Security",
    "manager": "Management",
    "director": "Management",
    "vp": "Management",
}


def classify_category(role: str) -> str:
    r = role.lower()
    for keyword, category in CATEGORY_MAP.items():
        if keyword in r:
            return category
    return "Other"


def classify_job(job: dict) -> dict:
    width = job.get("max", 0) - job.get("min", 0)
    job["_width"] = width
    job["_compliant"] = width <= 50_000
    job["_nonCompliant"] = width > 50_000
    job["_exempt"] = False
    job["_federal"] = False
    if "category" not in job or not job["category"]:
        job["category"] = classify_category(job.get("role", ""))
    return job


def validate_link(url: str, timeout: int = 10) -> bool:
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (compatible; payhub-bot/1.0)")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except Exception:
        return False


def load_existing() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return {"meta": {}, "jobs": []}


def save_data(db: dict):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE + ".tmp", "w") as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(DATA_FILE + ".tmp", DATA_FILE)


def main():
    db = load_existing()
    jobs = db.get("jobs", [])

    # Build dedup set from existing (role|company|url)
    existing_urls = {j.get("source_url", "") for j in jobs}
    existing_keys = {
        f"{j['role'].lower().strip()}|{j['company'].lower().strip()}"
        for j in jobs
    }

    # Parse all raw files
    raw_files = sorted(glob.glob(RAW_PATTERN))
    print(f"Found {len(raw_files)} raw file(s): {[os.path.basename(f) for f in raw_files]}")

    new_jobs = []
    for raw_file in raw_files:
        with open(raw_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if job.get("source_url") in existing_urls:
                    continue
                key = f"{job.get('role','').lower().strip()}|{job.get('company','').lower().strip()}"
                if key in existing_keys:
                    continue

                # Validate
                try:
                    val_min = int(job["min"])
                    val_max = int(job["max"])
                except (KeyError, ValueError):
                    continue
                if not (30_000 <= val_min < val_max <= 1_500_000):
                    continue

                job = classify_job(job)
                job["id"] = re.sub(r'[^a-z0-9]', '-', job.get('role','').lower()[:20]) + '-' + \
                             re.sub(r'[^a-z0-9]', '-', job.get('company','').lower()[:15]) + '-' + \
                             str(int(time.time() * 1000))[-8:]
                job["status"] = "active"
                job["date_added"] = TODAY
                if "posted" not in job:
                    job["posted"] = TODAY

                new_jobs.append(job)
                existing_keys.add(key)
                existing_urls.add(job.get("source_url", ""))

    print(f"New jobs from raw files: {len(new_jobs)}")

    # Archive dead links (check active jobs older than 7 days, sample 30)
    archived_count = 0
    active_jobs = [j for j in jobs if j.get("status") == "active"]
    to_check = [j for j in active_jobs if j.get("date_added", "") < TODAY][:30]
    for j in to_check:
        url = j.get("source_url", "")
        if url and not validate_link(url):
            j["status"] = "archived"
            j["archived_date"] = TODAY
            archived_count += 1

    # Merge
    jobs = jobs + new_jobs

    # Reapply classification to all active jobs
    for j in jobs:
        if j.get("status") == "active":
            j = classify_job(j)

    active_count = sum(1 for j in jobs if j.get("status") == "active")

    db["jobs"] = jobs
    db["meta"] = {
        "count": len(jobs),
        "active": active_count,
        "new_today": len(new_jobs),
        "links_newly_archived": archived_count,
        "updated": TIMESTAMP,
        "state": "Washington State",
        "law": "WA RCW 49.58.110",
        "currency": "USD",
    }

    save_data(db)
    print(f"Saved: {len(jobs)} total, {active_count} active, {len(new_jobs)} new, {archived_count} archived")

    # Clean up processed raw files
    for f in raw_files:
        try:
            os.remove(f)
        except OSError:
            pass

    return len(new_jobs)


if __name__ == "__main__":
    n = main()
    sys.exit(0)
