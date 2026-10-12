"""Letterboxd per-film ratings.

The community average (e.g. 4.35 / 5) is *not* on the list pages -- those only
carry `data-owner-rating`, the list owner's own score, which is 0 for unrated
films. The average is only available on the film page, as JSON-LD:

    "ratingValue": "4.35", "ratingCount": "1455741"

Verified live in v1.1.2 development against
`letterboxd.com/film/the-thing/` (4.35 from 1,455,741 ratings).

Fetching a film page per candidate is slow and expensive, so:
  * requests run concurrently, bounded by a polite slot limiter;
  * results are cached on disk for a week, so steady-state costs nothing;
  * a failed lookup returns None and simply leaves the rating unset.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import settings

log = logging.getLogger("watchlistarr.letterboxd_rating")

CACHE_DB = "letterboxd_ratings"
_CACHE_DAYS = 7

# One slot, so film-page fetches stay polite to Letterboxd.
_MIN_INTERVAL_SEC = 0.4
_lock = threading.Lock()
_last_call = 0.0

_rating_re = re.compile(r'"ratingValue"\s*:\s*"?([0-9.]+)"?')
_count_re = re.compile(r'"ratingCount"\s*:\s*"?([0-9]+)"?')


def _cache_path() -> Path:
    settings.ensure_dirs()
    return settings.data_dir / f"{CACHE_DB}.db"


def _conn() -> sqlite3.Connection:
    conn = getattr(_conn, "_c", None)
    if conn is None:
        conn = sqlite3.connect(_cache_path(), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ratings ("
            " slug TEXT PRIMARY KEY, value REAL, votes INTEGER, fetched_at TEXT)"
        )
        conn.commit()
        _conn._c = conn  # type: ignore[attr-defined]
    return conn


def cached(slug: str) -> tuple[float, int] | None:
    """Return (rating, votes) from cache, or None when stale/absent."""
    row = _conn().execute(
        "SELECT value, votes, fetched_at FROM ratings WHERE slug=?", (slug,)
    ).fetchone()
    if row is None:
        return None
    # db.now() writes an ISO-8601 string WITH a timezone offset ("...+00:00").
    # Parsing it with the naive format silently failed, so the cache never hit
    # and every film was re-fetched on every run.
    stamp = row["fetched_at"]
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds()
    if age > _CACHE_DAYS * 86400:
        return None
    try:
        return float(row["value"]), int(row["votes"] or 0)
    except (TypeError, ValueError):
        return None


def _store(slug: str, value: float, votes: int) -> None:
    from ..db import now

    _conn().execute(
        "INSERT INTO ratings(slug, value, votes, fetched_at) VALUES(?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET value=excluded.value, "
        "votes=excluded.votes, fetched_at=excluded.fetched_at",
        (slug, value, votes, now()),
    )
    _conn().commit()


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL_SEC - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def fetch_rating(slug: str, timeout: int | None = None) -> tuple[float, int] | None:
    """Community rating for a Letterboxd film slug, or None if unavailable."""
    slug = (slug or "").strip()
    if not slug:
        return None

    hit = cached(slug)
    if hit:
        return hit

    url = f"https://letterboxd.com/film/{slug}/"
    try:
        _throttle()
        with httpx.Client(
            timeout=timeout or settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        log.debug("letterboxd rating fetch failed for %s: %s", slug, exc)
        return None

    if resp.status_code != 200:
        return None

    m = _rating_re.search(resp.text)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    votes = 0
    cm = _count_re.search(resp.text)
    if cm:
        try:
            votes = int(cm.group(1))
        except ValueError:
            votes = 0

    _store(slug, value, votes)
    return value, votes


def fetch_many(slugs: list[str], workers: int = 4) -> dict[str, tuple[float, int]]:
    """Fetch several ratings concurrently. Cached entries cost nothing."""
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, tuple[float, int]] = {}
    todo: list[str] = []
    for s in slugs:
        s = (s or "").strip()
        if not s or s in out:
            continue
        hit = cached(s)
        if hit:
            out[s] = hit
        elif s not in todo:
            todo.append(s)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for res in pool.map(lambda s: (s, fetch_rating(s)), todo):
                slug, val = res
                if val:
                    out[slug] = val
    return out