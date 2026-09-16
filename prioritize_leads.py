#!/usr/bin/env python3
"""
Tech Bros Website Studio — Lead Prioritizer
================================================
Runs BETWEEN scraper.py and outreach.py.

Reads today's new_leads CSV, scores each lead (no website > bad website >
has decent website), figures out a specific one-line "gap" to open the
pitch with (e.g. "has 40 reviews and a 4.8 rating but no website"), and
writes a new, sorted CSV that outreach.py should call in order.

Usage:
    python prioritize_leads.py [path_to_leads_csv]
    (defaults to today's data/new_leads_YYYY-MM-DD.csv if no path given)

Output:
    data/new_leads_prioritized_YYYY-MM-DD.csv
    Same columns as the input, plus: priority_score, pitch_gap, recommended_pitch
    Sorted highest priority_score first — call/email these top-down.
"""

import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
LOCAL_TZ = ZoneInfo("America/Chicago")

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def todays_leads_path() -> Path:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    return DATA_DIR / f"new_leads_{today}.csv"


def safe_float(value, default=0.0):
    """Tolerates both clean numbers and older CSVs holding '4.2 stars'."""
    match = NUMBER_RE.search(str(value or "").replace(",", ""))
    return float(match.group(0)) if match else default


def safe_int(value, default=0):
    match = NUMBER_RE.search(str(value or "").replace(",", ""))
    return int(float(match.group(0))) if match else default


def score_and_pitch(row: dict) -> tuple:
    """Returns (priority_score, pitch_gap, recommended_pitch) for one lead."""
    website = (row.get("website") or "").strip()
    rating = safe_float(row.get("rating"))
    review_count = safe_int(row.get("review_count"))
    lead_priority = (row.get("lead_priority") or "").strip().upper()

    score = 0

    # The actor grades leads as High / Medium / Low
    if "HIGH" in lead_priority or "HOT" in lead_priority:
        score += 100
    elif "MEDIUM" in lead_priority or "WARM" in lead_priority:
        score += 50
    else:
        score += 10

    if not website:
        # No website is the strongest pitch angle — these are your best leads
        score += 50
        recommended_pitch = "website"
        if rating >= 4.5 and review_count >= 10:
            pitch_gap = (
                f"has {review_count} reviews and a {rating} rating but no website "
                f"to show it off"
            )
            score += 20  # great reputation + no website = your best possible lead
        elif review_count >= 5:
            pitch_gap = f"has {review_count} reviews on Google but no website listed"
        else:
            pitch_gap = "doesn't have a website listed on Google yet"
    else:
        # Has a website already — skip the website pitch, lead with AI services instead
        score += 5
        recommended_pitch = "ai_services"
        pitch_gap = "already has a website, so the opening should focus on AI voice/chat agents instead"

    return score, pitch_gap, recommended_pitch


def main():
    leads_path = Path(sys.argv[1]) if len(sys.argv) > 1 else todays_leads_path()
    if not leads_path.exists():
        sys.exit(f"Leads file not found: {leads_path}. Run scraper.py first, or pass a CSV path as an argument.")

    with open(leads_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        original_fieldnames = reader.fieldnames or []

    if not rows:
        print("No leads in that file — nothing to prioritize.")
        return

    for row in rows:
        score, pitch_gap, recommended_pitch = score_and_pitch(row)
        row["priority_score"] = score
        row["pitch_gap"] = pitch_gap
        row["recommended_pitch"] = recommended_pitch

    rows.sort(key=lambda r: r["priority_score"], reverse=True)

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    output_path = DATA_DIR / f"new_leads_prioritized_{today}.csv"
    fieldnames = original_fieldnames + ["priority_score", "pitch_gap", "recommended_pitch"]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Prioritized {len(rows)} leads -> {output_path}")
    print("\nTop 5 by priority:")
    for row in rows[:5]:
        print(f"  [{row['priority_score']:>3}] {row.get('business_name', '?'):<35} — {row['pitch_gap']}")


if __name__ == "__main__":
    main()
