#!/usr/bin/env python3
"""
Tech Bros AI Studio — Automated Outreach
================================================
Runs AFTER scraper.py. Reads today's new leads and, for each one:
    - If a phone number exists -> triggers an outbound AI call via Retell AI
    - Else if a website exists -> tries to find a contact email on the site,
      and sends an automatic outreach email if one is found
    - Else -> logs it to data/manual_followup.csv (no auto-contact possible)

Setup:
    1. Create a Retell AI account: https://retellai.com
    2. Create an Agent in the Retell dashboard using retell_agent_prompt.txt
       as your prompt starting point
    3. Buy/connect a phone number in Retell to call FROM
    4. Copy outreach_config.example.json -> outreach_config.json and fill it in
    5. Put RETELL_API_KEY (and EMAIL_ADDRESS / EMAIL_APP_PASSWORD if you enable
       email) in the .env file — see .env.example
    6. Run: python outreach.py --dry-run     <- ALWAYS do this first
       Then, once the dry run looks right:
          python outreach.py [path_to_leads_csv]
       (defaults to today's prioritized CSV, falling back to the plain one)

IMPORTANT — please read before turning this fully on:
    - Without --dry-run this makes REAL phone calls and sends REAL emails with
      no human review step. Start with a small test batch (max_calls_per_run
      in the config) before letting it run on a full daily list.
    - Businesses already contacted (per data/outreach_log.csv) and any number
      or domain listed in data/do_not_contact.csv are skipped automatically.
    - Outbound AI calling to businesses may be subject to telemarketing rules
      (e.g. TCPA in the US) depending on your state and how the calls are
      used. This script defaults to only calling within a configurable
      business-hours window, and the Retell agent prompt template identifies
      itself as calling on behalf of Tech Bros AI Studio. You're
      responsible for confirming compliance for your area — consider a quick
      check with a lawyer once you scale beyond testing.
    - Automatic emailing is OFF by default (enable_email in the config).
      US CAN-SPAM requires a working opt-out and a physical postal address in
      commercial email; the template must carry both before you switch it on.
"""

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from enrich import is_public_url, retell_variables

load_dotenv()

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "outreach_config.json"
DATA_DIR = ROOT / "data"
OUTREACH_LOG = DATA_DIR / "outreach_log.csv"
MANUAL_FOLLOWUP = DATA_DIR / "manual_followup.csv"
DO_NOT_CONTACT = DATA_DIR / "do_not_contact.csv"

RETELL_CREATE_CALL_URL = "https://api.retellai.com/v2/create-phone-call"
RETELL_GET_CALL_URL = "https://api.retellai.com/v2/get-call/{call_id}"
RETELL_STOP_CALL_URL = "https://api.retellai.com/v2/stop-call/{call_id}"

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

LOG_FIELDNAMES = ["timestamp", "business_name", "contact_method", "contact_value", "status", "detail"]

MANUAL_FIELDNAMES = ["timestamp", "business_name", "phone", "website", "city_searched", "reason"]

# Addresses that are never a real business contact — role accounts, vendor and
# tracking addresses that routinely appear in page source.
EMAIL_DENYLIST = (
    "example.com", "sentry", "wixpress", "squarespace", "godaddy", "shopify",
    "cloudflare", "google-analytics", "facebook", "doubleclick", ".png", ".jpg",
    ".jpeg", ".gif", ".svg", ".webp", "@sentry.io", "abuse@", "postmaster@",
)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH}. Copy outreach_config.example.json to outreach_config.json and edit it.")
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    for key in ("retell_agent_id", "retell_from_number", "email_subject", "email_body_template"):
        if not config.get(key):
            sys.exit(f"outreach_config.json is missing required key: {key}")
    if "PASTE_YOUR" in config["retell_agent_id"] or "XXXX" in config["retell_from_number"]:
        sys.exit("outreach_config.json still has placeholder Retell values — fill in your real agent ID and number.")
    if config.get("enable_email") and not config.get("company_postal_address"):
        sys.exit("enable_email is on, so company_postal_address is required (CAN-SPAM). Add it to outreach_config.json.")
    return config


def local_tz(config: dict) -> ZoneInfo:
    return ZoneInfo(config.get("timezone", "America/Chicago"))


def todays_leads_path(config: dict) -> Path:
    """Prefer the prioritized CSV so leads are contacted best-first."""
    today = datetime.now(local_tz(config)).strftime("%Y-%m-%d")
    prioritized = DATA_DIR / f"new_leads_prioritized_{today}.csv"
    return prioritized if prioritized.exists() else DATA_DIR / f"new_leads_{today}.csv"


def load_leads(path: Path) -> list:
    if not path.exists():
        sys.exit(f"Leads file not found: {path}. Run scraper.py first, or pass a CSV path as an argument.")
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_enrichment() -> dict:
    """Gemini's per-lead content is written to the database by enrich.py, but
    leads are read from CSV — which has no ai_* columns. Without this lookup
    retell_variables() sends an empty pitch_line on every call and the agent
    falls back to its generic script.

    Keyed by normalized phone. Imported lazily and failure-tolerant on purpose:
    db pulls in scraper (and apify_client), so a missing database or a minimal
    install degrades to CSV-only calling rather than stopping the run.
    """
    try:
        import db
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT phone, ai_call_opener, ai_summary, ai_hooks FROM leads"
                " WHERE ai_call_opener IS NOT NULL AND ai_call_opener != ''"
            ).fetchall()
    except Exception:
        return {}

    enriched = {}
    for row in rows:
        key = clean_phone(row["phone"])
        if key:
            enriched[key] = {"ai_call_opener": row["ai_call_opener"],
                             "ai_summary": row["ai_summary"],
                             "ai_hooks": row["ai_hooks"]}
    return enriched


def within_business_hours(config: dict) -> bool:
    now = datetime.now(local_tz(config))
    start = config.get("business_hours_start", 9)
    end = config.get("business_hours_end", 17)
    return start <= now.hour < end and now.weekday() < 5  # Mon-Fri only


def load_already_contacted() -> set:
    """Phone numbers and emails we've already reached, so a re-run never double-calls."""
    contacted = set()
    if OUTREACH_LOG.exists():
        with open(OUTREACH_LOG, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") in ("CALLED", "EMAILED"):
                    contacted.add((row.get("contact_value") or "").strip().lower())
    return contacted


def load_do_not_contact() -> set:
    """Opt-outs. One phone number, email, or domain per line; '#' starts a comment."""
    blocked = set()
    if DO_NOT_CONTACT.exists():
        with open(DO_NOT_CONTACT, "r", encoding="utf-8") as f:
            for line in f:
                value = line.split("#", 1)[0].strip().lower()
                if value:
                    blocked.add(value)
    return blocked


def is_blocked_email(email: str, blocked: set) -> bool:
    """A do-not-contact entry may be a whole domain, not just one address."""
    email = email.lower()
    return email in blocked or email.split("@")[-1] in blocked


def clean_phone(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    if len(digits) == 10:
        digits = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        digits = "+" + digits
    elif not digits.startswith("+"):
        return None
    return digits


def trigger_retell_call(config: dict, lead: dict, phone: str, dry_run: bool) -> tuple:
    api_key = os.environ.get("RETELL_API_KEY")
    if not api_key and not dry_run:
        return "SKIPPED", "RETELL_API_KEY not set"

    # Every variable the agent prompt references must be sent, or Retell speaks
    # the raw "{{placeholder}}" text out loud on the call. retell_variables()
    # adds the Gemini content when a lead has been enriched, and falls back to
    # the deterministic pitch_gap when it hasn't.
    payload = {
        "from_number": config["retell_from_number"],
        "to_number": phone,
        "override_agent_id": config["retell_agent_id"],
        "retell_llm_dynamic_variables": retell_variables(lead),
    }
    if dry_run:
        return "DRY_RUN", json.dumps(payload["retell_llm_dynamic_variables"])
    try:
        resp = requests.post(
            RETELL_CREATE_CALL_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return "CALLED", resp.json().get("call_id", "")
        return "FAILED", f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return "FAILED", str(e)


def stop_call(call_id: str) -> tuple:
    """Hang up a call that's still in progress. Used by the dashboard's Stop
    button — the agent ends calls on its own, so this is for when a human
    watching it decides to pull the plug early."""
    api_key = os.environ.get("RETELL_API_KEY")
    if not api_key:
        return False, "RETELL_API_KEY not set"
    if not call_id:
        return False, "no call_id to stop"
    try:
        resp = requests.post(
            RETELL_STOP_CALL_URL.format(call_id=call_id),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, str(exc)
    # 204 is the documented success; 200 accepted too in case that changes.
    if resp.status_code in (200, 204):
        return True, "call stopped"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def wait_for_call_to_end(call_id: str, api_key: str, poll_interval: int, max_wait: int) -> str:
    """Block until Retell reports the call has ended, so the next lead isn't
    dialed while this one is still live on the same from_number — calls are
    triggered one at a time, but Retell runs them async, so without this the
    script moves on after the API accepts the call, not after it finishes.

    Falls back to a time-based cutoff if status never settles (a lost
    webhook, a stuck call) so one bad call can't hang the whole run forever —
    max_call_wait_seconds should be set a bit above whatever max call
    duration is configured in Retell's agent settings.
    """
    if not call_id:
        return "unknown (no call_id — couldn't confirm it ended)"

    deadline = time.monotonic() + max_wait
    consecutive_errors = 0
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            resp = requests.get(
                RETELL_GET_CALL_URL.format(call_id=call_id),
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            consecutive_errors = 0
            if resp.status_code == 200:
                body = resp.json()
                call_status = body.get("call_status")
                reason = body.get("disconnection_reason")
                if call_status in ("ended", "error"):
                    return call_status
                # not_connected covers two different things: the call still
                # setting up (reason not set yet) and a no-answer/busy/failed
                # dial that's genuinely over (reason set, e.g.
                # dial_no_answer). Only the second is terminal — without this
                # check, a lead who just doesn't pick up blocks the run for
                # the full max_wait instead of freeing up in a few seconds.
                if call_status == "not_connected" and reason:
                    return reason
        except requests.RequestException:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                return "unknown (lost contact with Retell while polling)"
    return "timed out (still ongoing after max_call_wait_seconds — moving on anyway)"


def find_email_on_website(website: str) -> str | None:
    if not website:
        return None
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    if not is_public_url(website):
        return None

    pages_to_try = [website]
    for suffix in ("/contact", "/contact-us", "/about"):
        pages_to_try.append(website.rstrip("/") + suffix)

    for url in pages_to_try:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=False)
            if resp.status_code != 200:
                continue
            for match in EMAIL_RE.finditer(resp.text):
                email = match.group(0)
                if not any(bad in email.lower() for bad in EMAIL_DENYLIST):
                    return email
        except Exception:
            continue
    return None


def send_email(config: dict, to_email: str, business_name: str, dry_run: bool) -> tuple:
    sender = os.environ.get("EMAIL_ADDRESS")
    app_password = os.environ.get("EMAIL_APP_PASSWORD")
    if not dry_run and (not sender or not app_password):
        return "SKIPPED", "EMAIL_ADDRESS or EMAIL_APP_PASSWORD not set"

    # Plain replace, not .format() — the template is user-edited prose and a
    # stray brace in it would otherwise raise.
    subject = config["email_subject"].replace("{business_name}", business_name)
    body = config["email_body_template"].replace("{business_name}", business_name)
    body += (
        f"\n\n---\n{config['company_postal_address']}\n"
        f"Don't want to hear from us again? Reply with \"unsubscribe\" and we'll remove you immediately."
    )

    if dry_run:
        return "DRY_RUN", to_email

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    try:
        with smtplib.SMTP_SSL(config.get("smtp_server", "smtp.gmail.com"), config.get("smtp_port", 465)) as server:
            server.login(sender, app_password)
            server.sendmail(sender, [to_email], msg.as_string())
        return "EMAILED", to_email
    except Exception as e:
        return "FAILED", str(e)


def log_result(rows: list):
    file_exists = OUTREACH_LOG.exists()
    with open(OUTREACH_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def log_manual_followup(rows: list):
    if not rows:
        return
    file_exists = MANUAL_FOLLOWUP.exists()
    with open(MANUAL_FOLLOWUP, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANUAL_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Call/email today's new leads.")
    parser.add_argument("leads_csv", nargs="?", help="Defaults to today's prioritized leads CSV.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show exactly what would happen. Places no calls and sends no mail.")
    parser.add_argument("--limit", type=int, help="Override max_calls_per_run for this run.")
    args = parser.parse_args()

    config = load_config()
    leads_path = Path(args.leads_csv) if args.leads_csv else todays_leads_path(config)
    leads = load_leads(leads_path)

    DATA_DIR.mkdir(exist_ok=True)
    now = datetime.now(local_tz(config)).isoformat()

    already_contacted = load_already_contacted()
    blocked = load_do_not_contact()
    enrichment = load_enrichment()
    email_enabled = config.get("enable_email", False)
    max_calls = args.limit if args.limit is not None else config.get("max_calls_per_run", 5)
    pause = config.get("seconds_between_calls", 20)

    allow_calls = not config.get("enforce_business_hours", True) or within_business_hours(config)
    if not allow_calls:
        print("Outside configured business hours/days — phone leads go to manual follow-up.")

    banner = "DRY RUN — no calls placed, no mail sent" if args.dry_run else "LIVE RUN — real calls and emails"
    print(f"{banner}\nSource: {leads_path.name} ({len(leads)} leads) | call cap this run: {max_calls}\n")

    log_rows, manual_rows = [], []
    called = emailed = skipped_manual = 0

    for lead in leads:
        name = lead.get("business_name", "Unknown")
        phone = clean_phone(lead.get("phone", ""))
        website = (lead.get("website") or "").strip()
        if phone and phone in enrichment:
            lead = {**lead, **enrichment[phone]}

        def defer(reason):
            manual_rows.append({"timestamp": now, "business_name": name, "phone": phone or "",
                                "website": website, "city_searched": lead.get("city_searched", ""),
                                "reason": reason})
            print(f"  MANUAL {name:<40} ({reason})")

        if phone and (phone.lower() in blocked or phone.lower() in already_contacted):
            reason = "on do-not-contact list" if phone.lower() in blocked else "already contacted"
            defer(reason)
            skipped_manual += 1
            continue

        if phone and called >= max_calls:
            defer(f"call cap of {max_calls} reached this run")
            skipped_manual += 1
            continue

        # Re-checked per lead so a long run stops dialing the moment the window closes.
        if phone and (allow_calls and (not config.get("enforce_business_hours", True)
                                       or within_business_hours(config))):
            status, detail = trigger_retell_call(config, lead, phone, args.dry_run)
            if status == "CALLED":
                call_id = detail
                print(f"  CALL  {name:<40} {phone:<15} -> ringing (call_id={call_id})")
                end_status = wait_for_call_to_end(
                    call_id, os.environ.get("RETELL_API_KEY"),
                    config.get("call_poll_interval_seconds", 5),
                    config.get("max_call_wait_seconds", 360),
                )
                detail = f"call_id={call_id}; ended={end_status}"
                print(f"        {name:<40} {'':<15} -> {end_status}")
            else:
                print(f"  CALL  {name:<40} {phone:<15} -> {status}")
            log_rows.append({"timestamp": now, "business_name": name, "contact_method": "call",
                             "contact_value": phone, "status": status, "detail": detail})
            if status in ("CALLED", "DRY_RUN"):
                called += 1
                already_contacted.add(phone.lower())
            if status == "CALLED" and pause:
                time.sleep(pause)
            continue

        if phone:
            defer("has phone, outside business hours")
            skipped_manual += 1
            continue

        if not email_enabled:
            defer("no phone; email outreach disabled in config")
            skipped_manual += 1
            continue

        email = find_email_on_website(website) if website else None
        if email and not is_blocked_email(email, blocked) and email.lower() not in already_contacted:
            status, detail = send_email(config, email, name, args.dry_run)
            log_rows.append({"timestamp": now, "business_name": name, "contact_method": "email",
                             "contact_value": email, "status": status, "detail": detail})
            if status in ("EMAILED", "DRY_RUN"):
                emailed += 1
                already_contacted.add(email.lower())
            print(f"  EMAIL {name:<40} {email:<30} -> {status}")
            continue

        defer("already contacted" if email else "no phone, no findable email")
        skipped_manual += 1

    if log_rows and not args.dry_run:
        log_result(log_rows)
    if manual_rows and not args.dry_run:
        log_manual_followup(manual_rows)

    verb = "would be triggered" if args.dry_run else "triggered"
    print(f"\nDone. Calls {verb}: {called} | Emails: {emailed} | Manual follow-up: {skipped_manual}")
    if args.dry_run:
        print("Nothing was sent and no logs were written. Re-run without --dry-run to go live.")
    else:
        print(f"Full log: {OUTREACH_LOG}")
        if manual_rows:
            print(f"Manual follow-up list: {MANUAL_FOLLOWUP}")


if __name__ == "__main__":
    main()
