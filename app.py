#!/usr/bin/env python3
"""Tech Bros Website Studio — lead scraper web UI.

Run locally:
    cp .env.example .env    # then fill in your token and password
    python app.py           # http://127.0.0.1:8080

scraper.py, prioritize_leads.py and the GitHub Action keep working unchanged:
this app reuses their functions and writes the same CSVs.
"""

import csv
import hmac
import io
import json
import os
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps

from apify_client import ApifyClient
from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)

load_dotenv()  # must run before anything reads os.environ below

import db
from enrich import check_budget, enrich_all
from prioritize_leads import score_and_pitch
from scraper import (DATA_DIR, FIELDNAMES, MASTER_FILE, lead_keys,
                     normalize_item, run_apify_search)

PRIORITIZED_FIELDNAMES = FIELDNAMES + ["priority_score", "pitch_gap", "recommended_pitch"]

COST_PER_RESULT = 0.01
MAX_TOTAL_RESULTS = 1000  # hard ceiling so a typo can't spend hundreds of dollars
MAX_PER_SEARCH = 200      # Apify's own per-run practical ceiling for this actor
PAGE_SIZE = 100

# Brute-force protection on the single shared password.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SECONDS = 300
_login_attempts = defaultdict(list)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # blocks cross-site POSTs that would spend Apify credit
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)


def daily_limit() -> float:
    return float(os.environ.get("DAILY_SPEND_LIMIT_USD") or 0)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def check_password(supplied: str) -> bool:
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        return False
    return hmac.compare_digest(supplied, expected)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def login_blocked(ip: str) -> int:
    """Seconds left on a lockout, or 0. Keeps the shared password from being
    guessable at machine speed once this is reachable off localhost."""
    cutoff = time.time() - LOGIN_LOCKOUT_SECONDS
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    if len(_login_attempts[ip]) >= LOGIN_MAX_ATTEMPTS:
        return int(_login_attempts[ip][0] + LOGIN_LOCKOUT_SECONDS - time.time())
    return 0


@app.route("/login", methods=["GET", "POST"])
def login():
    if not os.environ.get("APP_PASSWORD"):
        return render_template("login.html", setup_needed=True)

    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        wait = login_blocked(ip)
        if wait > 0:
            flash(f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).", "error")
            return render_template("login.html", setup_needed=False)

        if check_password(request.form.get("password", "")):
            _login_attempts.pop(ip, None)
            session["authed"] = True
            session.permanent = True
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") else url_for("dashboard"))

        _login_attempts[ip].append(time.time())
        remaining = LOGIN_MAX_ATTEMPTS - len(_login_attempts[ip])
        flash(f"Incorrect password. {remaining} attempt(s) before a temporary lockout."
              if remaining > 0 else "Too many failed attempts — locked out for 5 minutes.", "error")
    return render_template("login.html", setup_needed=False)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Scrape runner
# ---------------------------------------------------------------------------

def write_csvs(rows: list):
    """Mirror the files scraper.py produces, so the CLI and outreach.py agree."""
    DATA_DIR.mkdir(exist_ok=True)
    today = datetime.now(db.LOCAL_TZ).strftime("%Y-%m-%d")

    master_exists = MASTER_FILE.exists()
    with open(MASTER_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if not master_exists:
            writer.writeheader()
        writer.writerows(rows)

    for path, fields in ((DATA_DIR / f"new_leads_{today}.csv", FIELDNAMES),
                         (DATA_DIR / f"new_leads_prioritized_{today}.csv", PRIORITIZED_FIELDNAMES)):
        exists = path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerows(rows)


APIFY_MAX_ATTEMPTS = 3


def search_with_retry(client, keyword, city, want, lead_filter, min_rating, run_id):
    """One flaky actor run shouldn't cost you the whole search."""
    for attempt in range(1, APIFY_MAX_ATTEMPTS + 1):
        try:
            return run_apify_search(client, keyword, city, want, lead_filter, min_rating)
        except Exception:
            if attempt == APIFY_MAX_ATTEMPTS:
                raise
            delay = 2 ** attempt
            db.log(run_id, f"  retrying in {delay}s (attempt {attempt} of {APIFY_MAX_ATTEMPTS})", "warn")
            time.sleep(delay)


def run_scrape(run_id: int, searches: list, lead_filter: str, min_rating: float, token: str):
    client = ApifyClient(token)
    seen = db.existing_keys()
    today = datetime.now(db.LOCAL_TZ).strftime("%Y-%m-%d")
    collected = []
    failures = 0

    try:
        for entry in searches:
            keyword, city, want = entry["keyword"], entry["city"], entry["max_results"]

            if db.cancel_requested(run_id):
                db.log(run_id, "Cancelled — stopping before the next search.", "warn")
                db.finish_run(run_id, "cancelled")
                if collected:
                    write_csvs(collected)
                return

            budget_error = check_budget(want * COST_PER_RESULT)
            if budget_error:
                db.log(run_id, budget_error, "error")
                db.finish_run(run_id, "cancelled", budget_error)
                if collected:
                    write_csvs(collected)
                return

            db.log(run_id, f"Searching '{keyword}' in '{city}' (up to {want})…")
            try:
                items = search_with_retry(client, keyword, city, want, lead_filter, min_rating, run_id)
            except Exception as exc:
                failures += 1
                db.log(run_id, f"'{keyword}' in '{city}' failed: {exc}", "error")
                db.bump_progress(run_id)
                continue
            db.record_spend("apify", len(items), len(items) * COST_PER_RESULT)

            fresh, dupes = [], 0
            for item in items:
                row = normalize_item(item, keyword, city, today)
                keys = lead_keys(row)
                if not row["business_name"] or any(k in seen for k in keys):
                    dupes += 1
                    continue
                seen.update(keys)
                score, gap, pitch = score_and_pitch(row)
                row.update(priority_score=score, pitch_gap=gap, recommended_pitch=pitch)
                fresh.append(row)

            db.insert_leads(fresh, run_id)
            collected.extend(fresh)
            db.bump_progress(run_id, len(fresh), dupes)
            db.log(run_id, f"  → {len(fresh)} new, {dupes} already known")

        if collected:
            write_csvs(collected)

        if failures and failures == len(searches):
            db.log(run_id, "Every search failed — check your Apify token and account credit.", "error")
            db.finish_run(run_id, "error", "All searches failed")
            return

        suffix = f" ({failures} search(es) failed)" if failures else ""
        db.log(run_id, f"Finished. {len(collected)} new leads added.{suffix}", "success")
        db.finish_run(run_id, "done")
    except Exception as exc:
        db.log(run_id, f"Run failed: {exc}", "error")
        db.finish_run(run_id, "error", str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", stats=db.stats(), runs=db.list_runs(8),
                           top_leads=db.list_leads(limit=10),
                           spend=db.spend_summary(), limit_usd=daily_limit())


@app.route("/search", methods=["GET", "POST"])
@login_required
def search():
    defaults = load_scraper_config()
    page = lambda: render_template("search.html", defaults=defaults,
                                   cost_per=COST_PER_RESULT, max_total=MAX_TOTAL_RESULTS,
                                   max_per_search=MAX_PER_SEARCH)
    if request.method != "POST":
        return page()

    try:
        searches = build_searches(request.form)
    except ValueError as exc:
        flash(str(exc), "error")
        return page()

    lead_filter = request.form.get("lead_filter", "ALL")
    try:
        min_rating = float(request.form.get("min_rating", 0) or 0)
    except ValueError:
        flash("Minimum rating must be a number.", "error")
        return page()

    error = validate_searches(searches, lead_filter)
    if error:
        flash(error, "error")
        return page()

    projected = sum(s["max_results"] for s in searches) * COST_PER_RESULT
    budget_error = check_budget(projected)
    if budget_error:
        flash(budget_error, "error")
        return page()

    if db.active_run_id():
        flash("A scrape is already running — wait for it to finish or cancel it.", "error")
        return redirect(url_for("run_detail", run_id=db.active_run_id()))

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        flash("APIFY_TOKEN is not set — add it to your .env file and restart.", "error")
        return page()

    run_id = db.create_run(searches, lead_filter, min_rating)
    total = sum(s["max_results"] for s in searches)
    db.log(run_id, f"Starting {len(searches)} search(es), up to {total} results.")
    threading.Thread(
        target=run_scrape,
        args=(run_id, searches, lead_filter, min_rating, token),
        daemon=True,
    ).start()
    return redirect(url_for("run_detail", run_id=run_id))


def build_searches(form) -> list:
    """Both modes produce the same thing: a flat list of {keyword, city, max_results}.

    'grid' crosses every keyword with every city at one shared count.
    'custom' takes one row per search, each with its own count — so you can ask
    for 40 restaurants in Princeton and 5 dentists in Anna in the same run.
    """
    if form.get("mode") == "custom":
        keywords = form.getlist("row_keyword")
        cities = form.getlist("row_city")
        counts = form.getlist("row_count")
        searches = []
        for keyword, city, count in zip(keywords, cities, counts):
            keyword, city = keyword.strip(), city.strip()
            if not keyword and not city:
                continue  # a blank row the user left behind
            if not keyword or not city:
                raise ValueError("Every search row needs both a business type and a city.")
            try:
                want = int(count)
            except (TypeError, ValueError):
                raise ValueError(f"'{keyword} in {city}' needs a number for how many.")
            searches.append({"keyword": keyword, "city": city, "max_results": want})
        return searches

    try:
        shared = int(form.get("max_results", 2))
    except (TypeError, ValueError):
        raise ValueError("Results per search must be a number.")
    return [{"keyword": keyword, "city": city, "max_results": shared}
            for city in split_lines(form.get("cities", ""))
            for keyword in split_lines(form.get("keywords", ""))]


def validate_searches(searches: list, lead_filter: str) -> str | None:
    if not searches:
        return "Add at least one business type and one city."
    if lead_filter not in ("ALL", "HOT_ONLY", "HOT_WARM"):
        return "Unknown lead filter."
    for entry in searches:
        if not 1 <= entry["max_results"] <= MAX_PER_SEARCH:
            return (f"'{entry['keyword']} in {entry['city']}' asks for {entry['max_results']} — "
                    f"each search must be between 1 and {MAX_PER_SEARCH}.")
    total = sum(entry["max_results"] for entry in searches)
    if total > MAX_TOTAL_RESULTS:
        return (f"That's {total:,} results (about ${total * COST_PER_RESULT:,.2f}), over the "
                f"{MAX_TOTAL_RESULTS:,} safety limit. Narrow the search.")
    return None


@app.route("/runs/<int:run_id>")
@login_required
def run_detail(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    return render_template("run.html", run=run, searches=json.loads(run["searches"]))


@app.route("/api/runs/<int:run_id>")
@login_required
def run_status(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    after = request.args.get("after", 0, type=int)
    lines = db.get_log(run_id, after)
    return {
        "status": run["status"],
        "done": run["done_searches"],
        "total": run["total_searches"],
        "new_leads": run["new_leads"],
        "duplicates": run["duplicate_leads"],
        "error": run["error"],
        "log": [{"id": r["id"], "ts": r["ts"], "level": r["level"], "message": r["message"]}
                for r in lines],
    }


@app.route("/runs/<int:run_id>/cancel", methods=["POST"])
@login_required
def cancel_run(run_id):
    db.request_cancel(run_id)
    flash("Cancelling after the current search finishes.", "info")
    return redirect(url_for("run_detail", run_id=run_id))


def run_enrichment(run_id: int, limit: int, force: bool):
    """Progress lives in the runs table, not memory, so a restart mid-run leaves
    a finished record instead of a page that spins forever."""
    try:
        stats = enrich_all(limit, force, dry_run=False, run_id=run_id)
        db.log(run_id, f"Finished. {stats['enriched']} enriched, "
                       f"{stats['drafts']} email drafts, {stats['failed']} failed.", "success")
        db.finish_run(run_id, "done")
    except Exception as exc:
        db.log(run_id, str(exc), "error")
        db.finish_run(run_id, "error", str(exc))


@app.route("/enrich", methods=["GET", "POST"])
@login_required
def enrich_page():
    if request.method == "POST":
        active = db.active_run_id("enrich")
        if active:
            flash("Enrichment is already running.", "error")
            return redirect(url_for("run_detail", run_id=active))
        if not os.environ.get("GEMINI_API_KEY"):
            flash("GEMINI_API_KEY is not set — add it to your .env file and restart.", "error")
            return redirect(url_for("enrich_page"))

        limit = request.form.get("limit", 25, type=int)
        force = request.form.get("force") == "1"
        run_id = db.create_run([{"limit": limit, "force": force}], kind="enrich", total=limit)
        db.log(run_id, f"Analyzing up to {limit} leads…")
        threading.Thread(target=run_enrichment, args=(run_id, limit, force), daemon=True).start()
        return redirect(url_for("run_detail", run_id=run_id))

    return render_template("enrich.html", stats=db.enrichment_stats(),
                           spend=db.spend_summary(), limit_usd=daily_limit(),
                           recent=db.list_runs(5),
                           has_key=bool(os.environ.get("GEMINI_API_KEY")))


@app.route("/drafts")
@login_required
def drafts():
    return render_template("drafts.html", drafts=db.list_drafts())


@app.route("/leads")
@login_required
def leads():
    filters = current_filters()
    page = max(1, request.args.get("page", 1, type=int))
    total = db.count_leads(**filters)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    return render_template(
        "leads.html",
        leads=db.list_leads(**filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE),
        filters=filters, page=page, pages=pages, total=total,
        cities=db.distinct_values("city_searched"),
        categories=db.distinct_values("category_searched"),
    )


@app.route("/leads.csv")
@login_required
def leads_csv():
    filters = current_filters()

    def rows():
        """Streamed a chunk at a time so a large export never sits in memory."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=PRIORITIZED_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        for row in db.iter_leads(**filters):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow({k: row[k] for k in PRIORITIZED_FIELDNAMES})
            yield buffer.getvalue()

    today = datetime.now(db.LOCAL_TZ).strftime("%Y-%m-%d")
    return Response(
        rows(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="leads_{today}.csv"'},
    )


def current_filters() -> dict:
    return {
        "search": request.args.get("q", "").strip(),
        "city": request.args.get("city", "").strip(),
        "category": request.args.get("category", "").strip(),
        "only_no_website": request.args.get("no_website") == "1",
    }


def split_lines(raw: str) -> list:
    """Accepts one entry per line or comma-separated."""
    parts = []
    for line in raw.replace(",", "\n").splitlines():
        value = line.strip()
        if value and value not in parts:
            parts.append(value)
    return parts


def load_scraper_config() -> dict:
    """Seed the form from config.json so the UI and the CLI start in sync."""
    from scraper import CONFIG_PATH
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    else:
        cfg = {}
    return {
        "keywords": cfg.get("keywords", []),
        "cities": cfg.get("cities", []),
        "max_results": cfg.get("max_results_per_query", 2),
        "lead_filter": cfg.get("lead_filter", "ALL"),
        "min_rating": cfg.get("min_rating", 0),
    }


@app.template_filter("short_time")
def short_time(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%b %d, %I:%M %p")
    except ValueError:
        return value


@app.template_filter("from_json")
def from_json(value):
    return json.loads(value) if value else []


if __name__ == "__main__":
    db.init_db()
    synced = db.sync_from_master_csv()
    if synced:
        print(f"Imported {synced} lead(s) from {MASTER_FILE.name}")
    if not os.environ.get("APP_PASSWORD"):
        print("\n!! APP_PASSWORD is not set — the UI will refuse logins until you set it:")
        print('   export APP_PASSWORD="something-only-you-know"\n')
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8080)), debug=False)
