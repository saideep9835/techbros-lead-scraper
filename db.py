#!/usr/bin/env python3
"""SQLite storage behind the web UI.

The CSVs in data/ remain the interchange format that scraper.py and the
GitHub Action read and write. This database is a queryable mirror of them:
every lead the web app finds is written to both, and leads_master.csv is
re-synced into here at startup so leads added by the CLI or the daily
Action still show up on the dashboard.
"""

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scraper import MASTER_FILE, name_key, phone_key

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "leads.db"
LOCAL_TZ = ZoneInfo("America/Chicago")

LEAD_COLUMNS = [
    "date_found", "business_name", "category_searched", "city_searched",
    "phone", "address", "website", "rating", "review_count",
    "lead_priority", "google_maps_url", "priority_score", "pitch_gap",
    "recommended_pitch",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 'scrape' or 'enrich'. Both share this table so progress, logging and
    -- cancellation survive an app restart instead of living in memory.
    kind               TEXT NOT NULL DEFAULT 'scrape',
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL,
    -- JSON list of {keyword, city, max_results}; each entry has its own count,
    -- so "40 restaurants in Princeton" and "5 dentists in Anna" coexist.
    searches           TEXT NOT NULL,
    lead_filter        TEXT,
    min_rating         REAL,
    total_searches     INTEGER NOT NULL DEFAULT 0,
    done_searches      INTEGER NOT NULL DEFAULT 0,
    new_leads          INTEGER NOT NULL DEFAULT 0,
    duplicate_leads    INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    cancel_requested   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL DEFAULT 'info',
    message  TEXT NOT NULL
);

-- key_phone and key_name are both UNIQUE, so INSERT OR IGNORE drops a lead that
-- collides on EITHER one. SQLite treats NULLs as distinct, so leads missing a
-- phone don't all collapse into each other.
CREATE TABLE IF NOT EXISTS leads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key_phone         TEXT UNIQUE,
    key_name          TEXT UNIQUE,
    run_id            INTEGER REFERENCES runs(id),
    date_found        TEXT,
    business_name     TEXT,
    category_searched TEXT,
    city_searched     TEXT,
    phone             TEXT,
    address           TEXT,
    website           TEXT,
    rating            REAL,
    review_count      INTEGER,
    lead_priority     TEXT,
    google_maps_url   TEXT,
    priority_score    INTEGER DEFAULT 0,
    pitch_gap         TEXT,
    recommended_pitch TEXT,
    -- Filled in by enrich.py (Gemini). enriched_at doubles as the cache marker:
    -- a lead with a timestamp is never re-analyzed, so we never pay twice.
    enriched_at         TEXT,
    ai_summary          TEXT,
    ai_hooks            TEXT,
    ai_call_opener      TEXT,
    draft_email_subject TEXT,
    draft_email_body    TEXT,
    enrich_error        TEXT
);

-- Every billable unit, so a daily ceiling can be enforced and the running
-- total is visible rather than discovered on an invoice.
CREATE TABLE IF NOT EXISTS spend (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    day     TEXT NOT NULL,
    service TEXT NOT NULL,
    units   INTEGER NOT NULL,
    usd     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_run   ON leads(run_id);
CREATE INDEX IF NOT EXISTS idx_log_run     ON run_log(run_id, id);
CREATE INDEX IF NOT EXISTS idx_spend_day   ON spend(day);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


ENRICH_COLUMNS = {
    "enriched_at": "TEXT", "ai_summary": "TEXT", "ai_hooks": "TEXT",
    "ai_call_opener": "TEXT", "draft_email_subject": "TEXT",
    "draft_email_body": "TEXT", "enrich_error": "TEXT",
}


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add columns to a table made by an
        # earlier version, so backfill any that are missing.
        existing = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
        for column, coltype in ENRICH_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {coltype}")
    # A run left "running" by a crash or restart can never finish.
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status='error', error='Interrupted by restart', finished_at=? "
            "WHERE status='running'",
            (now_iso(),),
        )


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

def create_run(searches: list, lead_filter: str = None, min_rating: float = None,
               kind: str = "scrape", total: int = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (kind, started_at, status, searches, lead_filter, min_rating,"
            " total_searches) VALUES (?,?,?,?,?,?,?)",
            (kind, now_iso(), "running", json.dumps(searches), lead_filter, min_rating,
             len(searches) if total is None else total),
        )
        return cur.lastrowid


def set_total(run_id: int, total: int):
    with connect() as conn:
        conn.execute("UPDATE runs SET total_searches=? WHERE id=?", (total, run_id))


def log(run_id: int, message: str, level: str = "info"):
    with connect() as conn:
        conn.execute(
            "INSERT INTO run_log (run_id, ts, level, message) VALUES (?,?,?,?)",
            (run_id, now_iso(), level, message),
        )


def bump_progress(run_id: int, new_leads: int = 0, duplicates: int = 0):
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET done_searches = done_searches + 1, new_leads = new_leads + ?,"
            " duplicate_leads = duplicate_leads + ? WHERE id = ?",
            (new_leads, duplicates, run_id),
        )


def finish_run(run_id: int, status: str, error: str | None = None):
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, now_iso(), run_id),
        )


def request_cancel(run_id: int):
    with connect() as conn:
        conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))


def cancel_requested(run_id: int) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def get_run(run_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def list_runs(limit: int = 20):
    with connect() as conn:
        return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_log(run_id: int, after_id: int = 0):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM run_log WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after_id)
        ).fetchall()


def active_run_id(kind: str = "scrape") -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM runs WHERE status='running' AND kind=? ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return row["id"] if row else None


# --------------------------------------------------------------------------
# Spend
# --------------------------------------------------------------------------

def record_spend(service: str, units: int, usd: float):
    with connect() as conn:
        conn.execute(
            "INSERT INTO spend (ts, day, service, units, usd) VALUES (?,?,?,?,?)",
            (now_iso(), datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"), service, units, usd),
        )


def spend_today() -> float:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    with connect() as conn:
        row = conn.execute("SELECT SUM(usd) AS total FROM spend WHERE day=?", (today,)).fetchone()
    return round(row["total"] or 0.0, 2)


def spend_summary() -> dict:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    with connect() as conn:
        by_service = conn.execute(
            "SELECT service, SUM(usd) AS usd, SUM(units) AS units FROM spend"
            " WHERE day=? GROUP BY service", (today,)
        ).fetchall()
        all_time = conn.execute("SELECT SUM(usd) AS total FROM spend").fetchone()
        recent = conn.execute(
            "SELECT day, SUM(usd) AS usd FROM spend GROUP BY day ORDER BY day DESC LIMIT 14"
        ).fetchall()
    return {
        "today": spend_today(),
        "all_time": round(all_time["total"] or 0.0, 2),
        "by_service": {r["service"]: {"usd": round(r["usd"], 2), "units": r["units"]}
                       for r in by_service},
        "recent": [dict(r) for r in recent],
    }


# --------------------------------------------------------------------------
# Leads
# --------------------------------------------------------------------------

def insert_leads(rows: list, run_id: int | None) -> int:
    """Insert leads, skipping any that match an existing business on phone or on
    name+street+city. Returns the number actually added."""
    if not rows:
        return 0
    placeholders = ",".join("?" * (len(LEAD_COLUMNS) + 3))
    added = 0
    with connect() as conn:
        for row in rows:
            values = [phone_key(row.get("phone")),
                      name_key(row.get("business_name"), row.get("address"),
                               row.get("city_searched")),
                      run_id] + [row.get(col) or None for col in LEAD_COLUMNS]
            cur = conn.execute(
                f"INSERT OR IGNORE INTO leads (key_phone, key_name, run_id,"
                f" {','.join(LEAD_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
            added += cur.rowcount
    return added


def existing_keys() -> set:
    with connect() as conn:
        rows = conn.execute("SELECT key_phone, key_name FROM leads").fetchall()
    return {k for row in rows for k in (row["key_phone"], row["key_name"]) if k}


def _lead_filters(search: str, city: str, category: str, only_no_website: bool):
    clauses, params = [], []
    if search:
        clauses.append("(business_name LIKE ? OR address LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if city:
        clauses.append("city_searched = ?")
        params.append(city)
    if category:
        clauses.append("category_searched = ?")
        params.append(category)
    if only_no_website:
        clauses.append("(website IS NULL OR website = '')")
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


def list_leads(search: str = "", city: str = "", category: str = "",
               only_no_website: bool = False, limit: int = 100, offset: int = 0):
    where, params = _lead_filters(search, city, category, only_no_website)
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM leads {where} ORDER BY priority_score DESC, id DESC"
            f" LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()


def count_leads(search: str = "", city: str = "", category: str = "",
                only_no_website: bool = False) -> int:
    where, params = _lead_filters(search, city, category, only_no_website)
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM leads {where}", params).fetchone()["n"]


def iter_leads(search: str = "", city: str = "", category: str = "",
               only_no_website: bool = False):
    """Streams rows for CSV export so a large result set never sits in memory."""
    where, params = _lead_filters(search, city, category, only_no_website)
    with connect() as conn:
        cursor = conn.execute(
            f"SELECT * FROM leads {where} ORDER BY priority_score DESC, id DESC", params)
        while True:
            chunk = cursor.fetchmany(500)
            if not chunk:
                return
            yield from chunk


def get_lead(lead_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def distinct_values(column: str) -> list:
    if column not in ("city_searched", "category_searched"):
        raise ValueError(f"not a filterable column: {column}")
    with connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} AS v FROM leads WHERE v != '' ORDER BY v"
        ).fetchall()
    return [r["v"] for r in rows if r["v"]]


def stats() -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*)                                                    AS total,
                      SUM(CASE WHEN website IS NULL OR website = '' THEN 1 ELSE 0 END) AS no_website,
                      SUM(CASE WHEN phone   IS NOT NULL AND phone != '' THEN 1 ELSE 0 END) AS callable,
                      SUM(CASE WHEN priority_score >= 120 THEN 1 ELSE 0 END)      AS hot,
                      AVG(NULLIF(rating, 0))                                      AS avg_rating
               FROM leads"""
        ).fetchone()
        by_city = conn.execute(
            "SELECT city_searched AS label, COUNT(*) AS n FROM leads"
            " WHERE label != '' GROUP BY label ORDER BY n DESC LIMIT 10"
        ).fetchall()
        by_category = conn.execute(
            "SELECT category_searched AS label, COUNT(*) AS n FROM leads"
            " WHERE label != '' GROUP BY label ORDER BY n DESC LIMIT 10"
        ).fetchall()
    return {
        "total": row["total"] or 0,
        "no_website": row["no_website"] or 0,
        "callable": row["callable"] or 0,
        "hot": row["hot"] or 0,
        "avg_rating": round(row["avg_rating"], 1) if row["avg_rating"] else None,
        "by_city": [dict(r) for r in by_city],
        "by_category": [dict(r) for r in by_category],
    }


def leads_needing_enrichment(limit: int = 50, force: bool = False) -> list:
    """Highest-priority leads first, so a capped run enriches the ones you'd
    actually call first. Already-enriched leads are skipped unless forced."""
    where = "" if force else "WHERE enriched_at IS NULL"
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM leads {where} ORDER BY priority_score DESC, id LIMIT ?", (limit,)
        ).fetchall()


def save_enrichment(lead_id: int, data: dict):
    with connect() as conn:
        conn.execute(
            "UPDATE leads SET enriched_at=?, ai_summary=?, ai_hooks=?, ai_call_opener=?,"
            " draft_email_subject=?, draft_email_body=?, enrich_error=NULL WHERE id=?",
            (now_iso(), data.get("summary"), json.dumps(data.get("hooks") or []),
             data.get("call_opener"), data.get("email_subject"), data.get("email_body"),
             lead_id),
        )


def save_enrichment_error(lead_id: int, message: str):
    """Records the failure without touching existing content — a quota error on
    a re-run must not wipe good notes we already had. enriched_at is left alone
    too, so the lead is retried next time rather than being marked done."""
    with connect() as conn:
        conn.execute("UPDATE leads SET enrich_error=? WHERE id=?", (message, lead_id))


def enrichment_stats() -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END)  AS enriched,
                      SUM(CASE WHEN enrich_error IS NOT NULL THEN 1 ELSE 0 END) AS failed,
                      SUM(CASE WHEN draft_email_body IS NOT NULL THEN 1 ELSE 0 END) AS drafts
               FROM leads"""
        ).fetchone()
    return {k: row[k] or 0 for k in ("total", "enriched", "failed", "drafts")}


def count_leads_without_phone() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE phone IS NULL OR phone = ''"
        ).fetchone()["n"]


def list_drafts(limit: int = 200) -> list:
    """Leads with no phone that got an email draft instead."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM leads WHERE draft_email_body IS NOT NULL AND draft_email_body != ''"
            " ORDER BY priority_score DESC, id LIMIT ?", (limit,)
        ).fetchall()


def sync_from_master_csv() -> int:
    """Pull in any leads written by scraper.py or the GitHub Action."""
    if not MASTER_FILE.exists():
        return 0
    with open(MASTER_FILE, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return insert_leads(rows, run_id=None)
