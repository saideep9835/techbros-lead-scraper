#!/usr/bin/env python3
"""Tech Bros Website Studio — lead enrichment via Gemini.

Runs AFTER a scrape. For each lead it reads the business's website (when there
is one), then asks Gemini for call-ready talking points:

    - has a phone number  -> an opener + hooks for the Retell agent
    - no phone number     -> a draft email, saved for review (never sent here)

Results are cached on the lead row: a lead with `enriched_at` set is never
re-analyzed, so the same business is never paid for twice.

Setup:
    Put GEMINI_API_KEY in your .env file (see .env.example).

Usage:
    python enrich.py                  # enrich up to 25 un-enriched leads
    python enrich.py --limit 100
    python enrich.py --force          # re-analyze leads already done
    python enrich.py --dry-run        # show what would be sent, call nothing
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
# `or` rather than a dict default: a key present but blank in .env would
# otherwise resolve to "" and be sent as the model name.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"

MAX_SITE_CHARS = 12000        # plenty for a small-business site; keeps tokens sane
MAX_DOWNLOAD_BYTES = 2_000_000  # stop reading a page rather than pull a huge file
REQUEST_TIMEOUT = 20
PAUSE_BETWEEN_CALLS = 1.0

MAX_ATTEMPTS = 4
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Rough per-lead estimate for the spend ledger; override in .env if your real
# invoice disagrees. Cost per lead is one Flash call over a truncated page.
GEMINI_COST_PER_LEAD = float(os.environ.get("GEMINI_COST_PER_LEAD") or 0.002)

SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "One or two sentences on what this business actually does."},
        "hooks": {"type": "array", "items": {"type": "string"},
                  "description": "2-4 specific, concrete talking points for this business."},
        "call_opener": {"type": "string",
                        "description": "One spoken sentence saying why we called this "
                                       "business, used AFTER they agree to talk. Not a greeting."},
        "email_subject": {"type": "string"},
        "email_body": {"type": "string"},
    },
    "required": ["summary", "hooks", "call_opener", "email_subject", "email_body"],
}


# ---------------------------------------------------------------------------
# Fetching the lead's website
# ---------------------------------------------------------------------------

def is_public_url(url: str) -> bool:
    """Lead URLs come from scraped data, so refuse anything that would make us
    fetch our own network — loopback, LAN, or cloud metadata addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


MAX_REDIRECTS = 4


def resolve_url(url: str) -> str | None:
    """Follow redirects by hand, re-checking each hop against is_public_url.

    Requests' own redirect following would skip that check, but refusing
    redirects outright breaks almost every real site — plain http:// nearly
    always 301s to https://.
    """
    for _ in range(MAX_REDIRECTS):
        if not is_public_url(url):
            return None
        try:
            resp = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=False,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; TechBrosLeadBot/1.0)"})
        except requests.RequestException:
            return url  # some hosts refuse HEAD; let the GET decide
        if resp.status_code not in (301, 302, 303, 307, 308):
            return url
        location = resp.headers.get("Location")
        if not location:
            return url
        url = urljoin(url, location)
    return None


def fetch_page_text(url: str) -> str:
    """Return the visible text of a page, or '' if it can't be read safely."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = resolve_url(url)
    if not url:
        return ""
    try:
        # Streamed so an enormous or endless response is cut off mid-download
        # rather than pulled fully into memory and truncated afterwards.
        with requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; TechBrosLeadBot/1.0)"}) as resp:
            if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", ""):
                return ""
            chunks, size = [], 0
            for chunk in resp.iter_content(65536, decode_unicode=True):
                if not chunk:
                    continue
                chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "ignore"))
                size += len(chunk)
                if size >= MAX_DOWNLOAD_BYTES:
                    break
            html = "".join(chunks)
    except requests.RequestException:
        return ""

    text = SCRIPT_STYLE_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    return " ".join(text.split())[:MAX_SITE_CHARS]


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def build_prompt(lead: dict, site_text: str) -> str:
    has_phone = bool((lead.get("phone") or "").strip())
    channel = ("They have a phone number, so this lead will be CALLED by an AI voice agent."
               if has_phone else
               "They have NO phone number, so this lead can only be reached by EMAIL.")

    has_website = bool((lead.get("website") or "").strip())
    if site_text:
        evidence = f"Here is the text of their website:\n---\n{site_text}\n---"
    elif has_website:
        # Critical: a failed fetch is NOT the same as having no website. Saying
        # "you don't have a website" to someone who does loses the call in the
        # first ten seconds.
        evidence = ("They DO have a website, but we could not read it automatically. "
                    "You know nothing about its contents. Do NOT say or imply they have no "
                    "website, and do NOT guess what is on it. Pitch AI tools that work "
                    "alongside an existing site, and ask about the site rather than "
                    "asserting anything about it.")
    else:
        evidence = ("They have NO website listed on Google — that is the single most "
                    "important fact about this lead, and the main reason we're contacting them.")

    angle = ("Lead with the website pitch." if lead.get("recommended_pitch") == "website"
             else "They already have a website, so do NOT offer to build one — lead with AI tools.")

    return f"""You are preparing outreach for Tech Bros Website Studio, a small studio that \
builds websites for local businesses and adds AI tools (chat assistants, voice agents that \
handle scheduling and FAQs).

The prospect:
- Name: {lead.get('business_name')}
- Type: {lead.get('category_searched')}
- Location: {lead.get('city_searched')}
- Google rating: {lead.get('rating') or 'unknown'} from {lead.get('review_count') or 'unknown'} reviews
- Website: {lead.get('website') or 'NONE FOUND'}

{channel}

{evidence}

{angle}

Write outreach material for this specific business. Requirements:
- Be concrete. Reference what THIS business actually does, not generic filler.
- Never invent facts. If the website says nothing about a detail, leave it out.
- Never assert anything about whether they have a website beyond what you were \
told above. Getting that wrong loses the call immediately.
- If they have no website, lead with what they're missing out on, using their \
review count and rating as evidence that they already have real customers.
- call_opener: this is NOT a greeting. The agent has already introduced itself \
and been given permission to talk. This is the one sentence that follows, saying \
why we called this particular business. Natural spoken English, no jargon, under \
30 words, and it should invite a reply rather than launch into a pitch.
- email_body: short and personal. No greeting line like "Dear Sir/Madam". \
Do not include a signature or unsubscribe line; those are added automatically.
- Honest and low-pressure. We are asking for a short conversation, not closing a sale."""


def call_gemini(prompt: str, api_key: str) -> dict:
    payload = {
        "model": GEMINI_MODEL,
        "input": prompt,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": RESPONSE_SCHEMA,
        },
    }

    # A quota blip or a 503 shouldn't lose the lead — back off and try again.
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                GEMINI_URL,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
        else:
            if resp.status_code == 200:
                break
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code not in RETRY_STATUSES:
                raise RuntimeError(f"Gemini {last_error}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(2 ** attempt)  # 2s, 4s, 8s
    else:
        raise RuntimeError(f"Gemini failed after {MAX_ATTEMPTS} attempts — {last_error}")

    body = resp.json()
    text = body.get("output_text")
    if not text:
        # output_text is a convenience field; fall back to walking the steps.
        parts = [block.get("text", "")
                 for step in body.get("steps", [])
                 for block in step.get("content", [])
                 if block.get("type") == "text"]
        text = "".join(parts)
    if not text:
        raise RuntimeError("Gemini returned no text output")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Per-lead orchestration
# ---------------------------------------------------------------------------

def analyze_lead(lead: dict, api_key: str) -> dict:
    site_text = fetch_page_text(lead.get("website") or "")
    result = call_gemini(build_prompt(lead, site_text), api_key)
    result["hooks"] = [h for h in (result.get("hooks") or []) if h][:4]

    # Their rule: a draft email is for leads we cannot phone.
    if (lead.get("phone") or "").strip():
        result["email_subject"] = None
        result["email_body"] = None
    return result


def retell_variables(lead: dict) -> dict:
    """Everything the Retell agent prompt can reference, as strings.

    Falls back to the deterministic pitch_gap when a lead hasn't been enriched,
    so calling never depends on Gemini having run.
    """
    hooks = []
    if lead.get("ai_hooks"):
        try:
            hooks = json.loads(lead["ai_hooks"])
        except (json.JSONDecodeError, TypeError):
            hooks = []
    return {
        "business_name": lead.get("business_name") or "there",
        "business_category": lead.get("category_searched") or "local",
        "recommended_pitch": lead.get("recommended_pitch") or "website",
        # The greeting is fixed in the agent prompt; this is what follows once
        # they've agreed to talk.
        "pitch_line": lead.get("ai_call_opener") or "",
        "business_summary": lead.get("ai_summary") or "",
        "talking_points": "; ".join(hooks) if hooks else (lead.get("pitch_gap") or ""),
    }


def enrich_all(limit: int, force: bool, dry_run: bool, run_id: int = None) -> dict:
    import db  # imported here so outreach.py can reuse is_public_url without pulling in the DB

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and not dry_run:
        raise RuntimeError("GEMINI_API_KEY is not set — add it to your .env file.")

    leads = db.leads_needing_enrichment(limit, force)
    if run_id:
        db.set_total(run_id, len(leads))
    done = failed = drafted = 0

    for lead in leads:
        row = dict(lead)
        name = row.get("business_name", "?")

        if run_id and db.cancel_requested(run_id):
            _report(run_id, "Cancelled — stopping here.", "warn")
            break

        budget_error = None if dry_run else check_budget(GEMINI_COST_PER_LEAD)
        if budget_error:
            _report(run_id, budget_error, "error")
            break

        succeeded = 0
        try:
            if dry_run:
                site = fetch_page_text(row.get("website") or "")
                note = f"{len(site)} chars of site text" if site else "no website to read"
                _report(run_id, f"  would analyze {name} ({note})")
                done += 1
                continue

            result = analyze_lead(row, api_key)
            db.save_enrichment(row["id"], result)
            db.record_spend("gemini", 1, GEMINI_COST_PER_LEAD)
            done += 1
            succeeded = 1
            if result.get("email_body"):
                drafted += 1
            channel = "email draft" if result.get("email_body") else "call notes"
            _report(run_id, f"  {name} → {channel}")
            time.sleep(PAUSE_BETWEEN_CALLS)
        except Exception as exc:
            failed += 1
            db.save_enrichment_error(row["id"], str(exc)[:300])
            _report(run_id, f"  {name} failed: {exc}", "error")
        finally:
            if run_id:
                db.bump_progress(run_id, succeeded, 0)

    return {"enriched": done, "failed": failed, "drafts": drafted, "considered": len(leads)}


def check_budget(next_cost: float) -> str | None:
    """Returns an error message if this call would break the daily ceiling."""
    import db
    cap = float(os.environ.get("DAILY_SPEND_LIMIT_USD") or 0)
    if cap <= 0:
        return None
    spent = db.spend_today()
    if spent + next_cost > cap:
        return (f"Daily spend limit reached — ${spent:.2f} of ${cap:.2f} used today. "
                f"Raise DAILY_SPEND_LIMIT_USD in .env or wait until tomorrow.")
    return None


def _report(run_id, message: str, level: str = "info"):
    if run_id:
        import db
        db.log(run_id, message, level)
    else:
        print(message)


def main():
    parser = argparse.ArgumentParser(description="Add Gemini call/email content to leads.")
    parser.add_argument("--limit", type=int, default=25, help="How many leads to process (default 25).")
    parser.add_argument("--force", action="store_true", help="Re-analyze leads already enriched.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch sites but call no API.")
    args = parser.parse_args()

    import db
    db.init_db()

    try:
        stats = enrich_all(args.limit, args.force, args.dry_run)
    except RuntimeError as exc:
        sys.exit(str(exc))

    if not args.dry_run:
        print(f"Spend today: ${db.spend_today():.2f}")

    if not stats["considered"]:
        print("Nothing to enrich — every lead already has content. Use --force to redo them.")
        return
    verb = "would enrich" if args.dry_run else "enriched"
    print(f"\n{verb.capitalize()}: {stats['enriched']} | email drafts: {stats['drafts']} "
          f"| failed: {stats['failed']}")


if __name__ == "__main__":
    main()
