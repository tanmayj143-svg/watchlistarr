"""Digital-release availability.

Skip films that have not been released digitally yet; reconsider them once they
have. Nothing is deleted when a film is filtered out — it is marked and
re-checked on every sync, so it becomes eligible the moment it ships.

Where the data comes from
-------------------------
* **With a TMDB key** (free, required since v1.7.0): `release_dates` is
  authoritative. It reports every release type, and `type 4` is *digital*.
* **Without a key** (misconfiguration since v1.7.0): no fallback metadata
  provider exists any more, so the release-date heuristic applies — a future
  date, or a current-year film with no date, is held back.

Heuristic (keyless)
-------------------
Treat a film as "not yet digitally available" when:

* its release date is in the future, or
* it has no release date at all but its year is the current or a later year.

That is deliberately the safe direction: a festival premiere with no digital
date yet is held back rather than requested prematurely. Releasing this filter
is a one-field change in Settings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("watchlistarr.release")

# TMDB release_type ids we treat as "digitally available"
TMDB_DIGITAL_TYPES = {4, 5, 6}  # 4=digital, 5=physical, 6=TV
TMDB_API = "https://api.themoviedb.org/3"

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass
class ReleaseStatus:
    """Availability verdict for one film."""

    available: bool
    confidence: str          # "tmdb" | "heuristic" | "unknown"
    digital_date: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "confidence": self.confidence,
            "digital_date": self.digital_date,
            "note": self.note,
        }


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    m = _DATE_RE.search(str(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _cache_path():
    settings.ensure_dirs()
    return settings.data_dir / "release_cache.db"


def _conn():
    conn = getattr(_conn, "_c", None)
    if conn is None:
        import sqlite3

        conn = sqlite3.connect(_cache_path(), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS releases ("
            " key TEXT PRIMARY KEY, available INTEGER, confidence TEXT,"
            " digital_date TEXT, checked_at TEXT)"
        )
        conn.commit()
        _conn._c = conn  # type: ignore[attr-defined]
    return conn


def cached(key: str, ttl_days: int = 7) -> ReleaseStatus | None:
    import time

    row = _conn().execute(
        "SELECT available, confidence, digital_date, checked_at FROM releases WHERE key=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        when = datetime.fromisoformat(str(row["checked_at"]))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    if (datetime.now(timezone.utc) - when).days > ttl_days:
        return None
    return ReleaseStatus(
        available=bool(row["available"]),
        confidence=str(row["confidence"]),
        digital_date=row["digital_date"],
        note="cached",
    )


def _store(key: str, status: ReleaseStatus) -> None:
    from ..db import now

    _conn().execute(
        "INSERT INTO releases(key, available, confidence, digital_date, checked_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET available=excluded.available, "
        "confidence=excluded.confidence, digital_date=excluded.digital_date, "
        "checked_at=excluded.checked_at",
        (key, int(status.available), status.confidence, status.digital_date, now()),
    )
    _conn().commit()


# --------------------------------------------------------------------------
# TMDB (accurate path)
# --------------------------------------------------------------------------
def tmdb_status(tmdb_id: Any, api_key: str, timeout: int | None = None) -> ReleaseStatus | None:
    if not tmdb_id or not api_key:
        return None
    try:
        tid = int(tmdb_id)
    except (TypeError, ValueError):
        return None

    key = f"tmdb:{tid}"
    hit = cached(key)
    if hit:
        return hit

    try:
        with httpx.Client(timeout=timeout or settings.http_timeout, follow_redirects=True) as c:
            r = c.get(
                f"{TMDB_API}/movie/{tid}",
                params={"api_key": api_key, "append_to_response": "release_dates"},
            )
        if r.status_code != 200:
            return None
        data = r.json() or {}
    except (httpx.HTTPError, ValueError):
        return None

    results = ((data.get("release_dates") or {}).get("results") or [])
    best: date | None = None
    saw_any_entry = False
    for country in results:
        for entry in country.get("release_dates") or []:
            saw_any_entry = True
            # BUG FIXED in v1.3.1: TMDB names this field "type", NOT
            # "release_type". Reading the wrong key made every film look like it
            # had no digital release, i.e. the filter blocked everything the
            # moment a valid TMDB key was configured.
            rtype = entry.get("type")
            if rtype is None:
                rtype = entry.get("release_type")
            if rtype in TMDB_DIGITAL_TYPES:
                d = _parse_date(entry.get("release_date"))
                if d and d.year > 1900 and (best is None or d < best):
                    best = d

    status_str = str(data.get("status") or "")
    if not saw_any_entry:
        # No release_dates payload at all -- do not pretend we know.
        return None
    if best:
        st = ReleaseStatus(
            available=best <= datetime.now(timezone.utc).date(),
            confidence="tmdb",
            digital_date=best.isoformat(),
        )
    elif status_str == "Released":
        # Released with no digital date recorded -> allow, but say so.
        st = ReleaseStatus(True, "tmdb", None, "released, no digital date on TMDB")
    else:
        st = ReleaseStatus(
            False, "tmdb", None, f"TMDB status={status_str or 'unknown'}, no digital release"
        )

    _store(key, st)
    return st


# --------------------------------------------------------------------------
# keyless heuristic
# --------------------------------------------------------------------------
def heuristic_status(
    released: str | None,
    year: int | None,
    today: date | None = None,
) -> ReleaseStatus:
    """Conservative keyless guess. Never claims more certainty than it has."""
    today = today or datetime.now(timezone.utc).date()
    d = _parse_date(released)

    if d:
        if d > today:
            return ReleaseStatus(
                False, "heuristic",
                note=f"releases {d.isoformat()}, not out yet",
            )
        return ReleaseStatus(True, "heuristic", d.isoformat(), "release date has passed")

    if year and year >= today.year:
        return ReleaseStatus(
            False, "heuristic",
            note=f"year {year} with no release date — likely festival-only so far",
        )
    if not year:
        return ReleaseStatus(False, "heuristic", note="no year or release date")
    return ReleaseStatus(True, "heuristic", None, "older film")


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def evaluate(
    *,
    tmdb_id: Any = None,
    released: str | None = None,
    year: int | None = None,
    tmdb_key: str | None = None,
    timeout: int | None = None,
) -> ReleaseStatus:
    """Release verdict for one film, using TMDB when possible.

    `timeout` is deliberately short on the bulk scoring path: this runs once per
    candidate, so one slow response per film turns a re-score of a few hundred
    films into minutes. A timeout degrades to the date heuristic, which is a
    lesser evil than never finishing.
    """
    if tmdb_key:
        st = tmdb_status(tmdb_id, tmdb_key, timeout=timeout)
        if st is not None:
            return st
    return heuristic_status(released, year)


WARNED = False


def warn_once_if_keyless(tmdb_key: str | None) -> None:
    """Tell the operator once per process that the check is heuristic."""
    global WARNED
    if not tmdb_key and not WARNED:
        WARNED = True
        log.warning(
            "No TMDB API key configured, so 'digitally released' is judged by a "
            "release-date heuristic, not real availability data. Films released "
            "early this year may be held back unnecessarily. Add a free TMDB key "
            "in Settings for accurate filtering."
        )