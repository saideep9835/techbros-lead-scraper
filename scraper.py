#!/usr/bin/env python3
"""
Tech Bros Website Studio — Daily Lead Scraper
================================================
Runs a Google Maps lead-generation search (via Apify) across a configurable
list of business keywords + cities, filters out leads you've already seen,
and saves the new ones to a CSV.

Setup:
    1. pip install -r requirements.txt
    2. Set the APIFY_TOKEN environment variable (see README.md)
    3. Edit config.json to set your keywords / cities / result limits
    4. Run:  python scraper.py

Designed to run either:
    - Locally (cron / Windows Task Scheduler)
    - On a schedule via GitHub Actions (see .github/workflows/daily-scrape.yml)
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
MASTER_FILE = DATA_DIR / "leads_master.csv"

ACTOR_ID = "ittechinnovators/google-maps-business-scraper-lead-generator"

FIELDNAMES = [
    "date_found",
    "business_name",
    "category_searched",
    "city_searched",
    "phone",
    "address",
    "website",
    "rating",
    "review_count",
    "lead_priority",
    "google_maps_url",
]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing config file: {CONFIG_PATH}. Copy config.example.json to config.json first.")
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_seen_keys() -> set:
    """Every identity key already on file, so we don't re-save the same business."""
    seen = set()
    if MASTER_FILE.exists():
        with open(MASTER_FILE, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                seen.update(lead_keys(row))
    return seen


# Legal suffixes and filler words that a listing gains or loses between scrapes.
NOISE_WORDS_RE = re.compile(r"\b(llc|l\.l\.c|inc|incorporated|co|corp|corporation|ltd|"
                            r"limited|the|and|of|dba)\b")


def _norm(value: str) -> str:
    """Collapse a scraped string to comparable letters and digits only."""
    text = PUA_RE.sub("", str(value or "")).lstrip("'").lower()
    text = text.replace("’", "'").replace("&", "and")
    text = NOISE_WORDS_RE.sub(" ", text)
    return re.sub(r"[^a-z0-9]", "", text)


def phone_key(phone: str) -> str | None:
    """Last 10 digits — the same line means the same business, whatever the
    listing calls itself this week."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return f"tel:{digits[-10:]}" if len(digits) >= 10 else None


def name_key(name: str, address: str, city: str) -> str | None:
    """Fallback identity: normalized name + street number + city. The street
    number survives 'St' -> 'Street', an added suite, and a dropped ZIP."""
    normalized = _norm(name)
    if not normalized:
        return None
    street = re.search(r"\d+", PUA_RE.sub("", str(address or "")))
    return f"biz:{normalized}|{street.group(0) if street else ''}|{_norm(city)}"


def lead_keys(row: dict) -> list:
    """All identities a lead can match on. Two rows are the same business if
    they share ANY key, so a renamed listing and a reformatted address both
    still collapse onto the existing record."""
    keys = [phone_key(row.get("phone")),
            name_key(row.get("business_name"), row.get("address"), row.get("city_searched"))]
    return [k for k in keys if k]


def run_apify_search(client: ApifyClient, keyword: str, city: str, max_results: int, lead_filter: str, min_rating: float):
    """Call the Apify actor for one keyword+city combo and return the raw items."""
    run_input = {
        "searchingKeywords": keyword,
        "location": city,
        "maxResults": max_results,
        "minRating": min_rating,
        "leadFilter": lead_filter,
    }
    print(f"  -> Searching '{keyword}' in '{city}' (max {max_results})...")
    run = client.actor(ACTOR_ID).call(run_input=run_input)
    items = list(client.dataset(run.default_dataset_id).iterate_items())
    print(f"     got {len(items)} results")
    return items


PUA_RE = re.compile(r"[-]")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def clean_text(value) -> str:
    """Maps fields arrive with icon-font glyphs and embedded newlines; a leading
    =/+/-/@ is also quoted so spreadsheets treat it as text, not a formula."""
    text = PUA_RE.sub("", str(value or ""))
    text = " ".join(text.split())
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        text = "'" + text
    return text


def parse_number(value, cast):
    """The actor reports these as prose — '4.2 stars', '87 reviews'."""
    match = NUMBER_RE.search(str(value or "").replace(",", ""))
    return cast(float(match.group(0))) if match else ""


def normalize_item(item: dict, keyword: str, city: str, today: str) -> dict:
    """Map the actor's output fields into our fixed CSV schema."""
    return {
        "date_found": today,
        "business_name": clean_text(item.get("name") or item.get("title") or item.get("businessName")),
        "category_searched": keyword,
        "city_searched": city,
        "phone": clean_text(item.get("phoneNumber") or item.get("phone")),
        "address": clean_text(item.get("address") or item.get("fullAddress")),
        "website": clean_text(item.get("websiteUrl") or item.get("website")),
        "rating": parse_number(item.get("rating") or item.get("totalScore"), float),
        "review_count": parse_number(item.get("reviewCount") or item.get("reviewsCount"), int),
        "lead_priority": clean_text(item.get("leadQuality") or item.get("leadPriority")),
        "google_maps_url": clean_text(item.get("googleMapUrl") or item.get("googleMapsUrl")),
    }


def main():
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("APIFY_TOKEN environment variable is not set. See README.md.")

    config = load_config()
    keywords = config["keywords"]
    cities = config["cities"]
    max_results_per_query = config.get("max_results_per_query", 5)
    lead_filter = config.get("lead_filter", "ALL")
    min_rating = config.get("min_rating", 0)

    DATA_DIR.mkdir(exist_ok=True)
    seen_keys = load_seen_keys()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    client = ApifyClient(token)

    all_new_rows = []
    print(f"Starting daily lead scrape — {len(keywords)} keyword(s) x {len(cities)} cities")

    for city in cities:
        for keyword in keywords:
            try:
                items = run_apify_search(client, keyword, city, max_results_per_query, lead_filter, min_rating)
            except Exception as e:
                print(f"     ERROR searching '{keyword}' in '{city}': {e}")
                continue

            for item in items:
                row = normalize_item(item, keyword, city, today)
                keys = lead_keys(row)
                if not row["business_name"] or any(k in seen_keys for k in keys):
                    continue
                seen_keys.update(keys)
                all_new_rows.append(row)

    if not all_new_rows:
        print("\nNo new leads found today (all results were duplicates of previous runs).")
        return

    # Append new leads to the master file (creating it with a header if needed)
    file_exists = MASTER_FILE.exists()
    with open(MASTER_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(all_new_rows)

    # Also write a dated "today's new leads only" file — handy for daily outreach
    todays_file = DATA_DIR / f"new_leads_{today}.csv"
    with open(todays_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_new_rows)

    print(f"\n✅ Found {len(all_new_rows)} new leads today.")
    print(f"   Added to: {MASTER_FILE}")
    print(f"   Today's batch: {todays_file}")


if __name__ == "__main__":
    main()
