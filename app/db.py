"""SQLite storage layer for Watchlistarr.

Deliberately plain sqlite3 (no ORM) so the container image stays tiny and the
schema is readable in the docs. WAL mode, busy timeout, and a JSON helper keep
the rest of the app simple.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import settings

log = logging.getLogger("watchlistarr.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS watchlists (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    source_ref    TEXT NOT NULL DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    accent        TEXT DEFAULT '#7c5cff',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    config        TEXT NOT NULL DEFAULT '{}',
    last_sync_at  TEXT,
    last_sync_stats TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id     INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    source_key       TEXT NOT NULL,
    title            TEXT NOT NULL,
    year             INTEGER,
    slug             TEXT,
    imdb_id          TEXT,
    tmdb_id          TEXT,
    poster           TEXT,
    overview         TEXT,
    genres           TEXT NOT NULL DEFAULT '[]',
    countries        TEXT NOT NULL DEFAULT '[]',
    languages        TEXT NOT NULL DEFAULT '[]',
    director         TEXT,
    runtime          INTEGER,
    imdb_rating      REAL,
    released         TEXT,          -- provider release date (ISO), for availability checks
    lb_rating        REAL,          -- Letterboxd community average (0-5)
    lb_votes         INTEGER,
    popularity       REAL,
    source_position  INTEGER,
    meta             TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'pending',
    score            REAL NOT NULL DEFAULT 0,
    breakdown        TEXT NOT NULL DEFAULT '{}',
    last_reason      TEXT,
    seerr_request_id INTEGER,
    seerr_state      TEXT,
    first_seen_at    TEXT NOT NULL,
    requested_at     TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE(watchlist_id, source_key)
);

CREATE INDEX IF NOT EXISTS idx_candidates_wl      ON candidates(watchlist_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status  ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_score   ON candidates(score DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_requested ON candidates(requested_at);

CREATE TABLE IF NOT EXISTS candidate_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_cand ON candidate_events(candidate_id);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id INTEGER REFERENCES watchlists(id) ON DELETE CASCADE,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    ok           INTEGER,
    stats        TEXT NOT NULL DEFAULT '{}',
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_wl ON runs(watchlist_id, id DESC);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """Thread-local connection (APScheduler + FastAPI share the process)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        settings.ensure_dirs()
        conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


# Columns added after v1.0.0, applied idempotently to existing databases.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("candidates", "lb_rating", "REAL"),
    ("candidates", "lb_votes", "INTEGER"),
    ("candidates", "released", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-migrate older databases in place (ALTER TABLE ADD COLUMN)."""
    for table, column, coltype in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            log.info("migrating %s: adding %s %s", table, column, coltype)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(args)).fetchall()


def one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(args)).fetchone()


def execute(sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = connect()
    cur = conn.execute(sql, tuple(args))
    conn.commit()
    return cur


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    conn = connect()
    conn.executemany(sql, [tuple(s) for s in seq])
    conn.commit()


def insert(sql: str, args: Iterable[Any] = ()) -> int:
    return int(execute(sql, args).lastrowid)


# --------------------------------------------------------------------------
# settings key/value
# --------------------------------------------------------------------------
def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), now()),
    )


def all_settings() -> dict[str, Any]:
    return {r["key"]: json.loads(r["value"]) for r in query("SELECT key, value FROM settings")}


# --------------------------------------------------------------------------
# row -> dict helpers
# --------------------------------------------------------------------------
def loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {} if default is None else default


def row_to_watchlist(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["source_ref"] = loads(d.get("source_ref"), {})
    d["config"] = loads(d.get("config"), {})
    d["enabled"] = bool(d.get("enabled"))
    d["last_sync_stats"] = loads(d.get("last_sync_stats"), {})
    return d


def row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # Alias so rule evaluation can use one name across providers.
    if d.get("lb_rating") is not None:
        d.setdefault("letterboxd_rating", d["lb_rating"])
    for f in ("genres", "countries", "languages"):
        d[f] = loads(d.get(f), [])
    d["breakdown"] = loads(d.get("breakdown"), {})
    d["meta"] = loads(d.get("meta"), {})
    return d