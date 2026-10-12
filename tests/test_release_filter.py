"""Digital-release filter tests (v1.3.0).

Covers the keyless heuristic and the TMDB-backed path, plus the requirement
that a held-back film is KEPT (not deleted) so it is reconsidered later.

Run: python tests/test_release_filter.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-rel-"))
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.metadata.release import (  # noqa: E402
    ReleaseStatus,
    _parse_date,
    evaluate,
    heuristic_status,
)

PASS = FAIL = 0
TODAY = date(2026, 10, 10)


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


print("\n1. Date parsing")
check("parses ISO date", _parse_date("1994-10-14T00:00:00.000Z") == date(1994, 10, 14))
check("parses plain date", _parse_date("2024-03-01") == date(2024, 3, 1))
check("rejects garbage", _parse_date("not a date") is None)
check("rejects None", _parse_date(None) is None)
check("rejects impossible date", _parse_date("2024-13-45") is None)


print("\n2. Heuristic: future release is held back")
st = heuristic_status("2027-01-15", 2027, today=TODAY)
check("future release -> unavailable", st.available is False, st.note)

st = heuristic_status("2026-12-01T00:00:00.000Z", 2026, today=TODAY)
check("release 2 months out -> unavailable", st.available is False, st.note)

print("\n3. Heuristic: past release is allowed")
st = heuristic_status("1994-10-14T00:00:00.000Z", 1994, today=TODAY)
check("1994 release -> available", st.available is True, st.note)

st = heuristic_status("2026-01-10", 2026, today=TODAY)
check("released earlier this year -> available", st.available is True, st.note)

# the boundary: released today is available
st = heuristic_status(TODAY.isoformat(), TODAY.year, today=TODAY)
check("released today -> available", st.available is True)

st = heuristic_status((TODAY + timedelta(days=1)).isoformat(), TODAY.year, today=TODAY)
check("releases tomorrow -> unavailable", st.available is False)


print("\n4. Heuristic: undated new films are held back (festival-only risk)")
st = heuristic_status(None, TODAY.year, today=TODAY)
check("current year, no date -> held back", st.available is False, st.note)

st = heuristic_status(None, TODAY.year + 1, today=TODAY)
check("next year, no date -> held back", st.available is False, st.note)

st = heuristic_status(None, TODAY.year - 1, today=TODAY)
check("last year, no date -> allowed", st.available is True, st.note)

st = heuristic_status(None, None, today=TODAY)
check("no year at all -> held back", st.available is False, st.note)


print("\n5. Confidence is always reported honestly")
for args in (("2030-01-01", 2030), ("1990-01-01", 1990), (None, 2030)):
    st = heuristic_status(*args, today=TODAY)
    check(f"heuristic labels confidence for {args}", st.confidence == "heuristic", st.confidence)

st = evaluate(released="2027-01-01", year=2027, tmdb_key=None)
check("evaluate() without a key -> heuristic", st.confidence == "heuristic")
check("evaluate() respects the verdict", st.available is False)


print("\n6. TMDB path is used only with a key")
st = evaluate(released="2027-01-01", year=2027, tmdb_key=None)
check("no key -> no TMDB lookup, heuristic only", st.confidence == "heuristic")

# With a key but no tmdb_id, it must fall back rather than crash or claim TMDB.
st = evaluate(released="2027-01-01", year=2027, tmdb_key="some-key")
check("key but no tmdb_id -> falls back to heuristic", st.confidence == "heuristic")

# With neither a date nor a year it must not accidentally allow the film.
st = evaluate(tmdb_id=None, released=None, year=None, tmdb_key=None)
check("no data at all -> held back", st.available is False, st.note)


print("\n7. ReleaseStatus serialises for the API/UI")
st = ReleaseStatus(False, "heuristic", "2027-01-01", "not out yet")
d = st.as_dict()
check("as_dict has all fields",
      set(d) == {"available", "confidence", "digital_date", "note"}, str(sorted(d)))
check("as_dict values preserved", d["available"] is False and d["digital_date"] == "2027-01-01")


print("\n8. Held-back films are kept, not deleted (integration)")
# The engine marks them ineligible with reason "not_released" and does not
# change the candidate row, so a later sync re-evaluates it.
from app import db as _db  # noqa: E402

_db.init_db()
from app import settings_service as _svc  # noqa: E402
from app import watchlists as _wl  # noqa: E402
from app import engine as _eng  # noqa: E402
from app.arr.radarr import RadarrCapacity  # noqa: E402

_svc.set_integration("filter_unreleased", True)
_svc.set_integration("tmdb_api_key", "")   # force the keyless path

_wl.create_watchlist(
    name="release-test", source_type="letterboxd",
    source_ref={"list_url": "https://letterboxd.com/x/list/y/"},
    config={"max_per_run": 1, "rules": [
        {"id": "g", "kind": "genre", "op": "in", "value": ["Drama"], "points": 50},
    ]},
)
t_wl = _wl.list_watchlists()[0]

fut_id = _db.insert(
    "INSERT INTO candidates(watchlist_id, source_key, title, year, genres, released,"
    " meta, source_position, first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
    (t_wl["id"], "lb:future", "Future Drama", 2028,
     _db.json.dumps(["Drama"]), "2028-06-01", _db.json.dumps({}), 0,
     _db.now(), _db.now()),
)
ok_id = _db.insert(
    "INSERT INTO candidates(watchlist_id, source_key, title, year, genres, released,"
    " meta, source_position, first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
    (t_wl["id"], "lb:past", "Past Drama", 1990,
     _db.json.dumps(["Drama"]), "1990-01-01", _db.json.dumps({}), 1,
     _db.now(), _db.now()),
)

cap = RadarrCapacity(available=True)
cap.free_bytes = 500 * 1024**3
cap.total_bytes = 1000 * 1024**3
cap.movie_count = 10

evaluated = {e["title"]: e for e in _eng.evaluate_watchlist(t_wl, cap)}
future = evaluated.get("Future Drama")
past = evaluated.get("Past Drama")

check("undated/future film is held back",
      future is not None and future["eligible"] is False)
check("hold-back reason is explained",
      future and "not digitally released" in future["rejected_by"]["reason"],
      future["rejected_by"]["reason"] if future else "")
check("released film is eligible", past and past["eligible"] is True)
check("released film scores normally", past and past["score"] == 50, str(past and past["score"]))

# the row must still exist so a later sync can reconsider it
still_there = _db.one("SELECT id, status FROM candidates WHERE id=?", (fut_id,))
check("held-back film is KEPT in the database", still_there is not None)
check("held-back film is not marked requested", still_there and still_there["status"] != "requested")

# simulate it shipping: update the date and re-evaluate
_db.execute("UPDATE candidates SET released=? WHERE id=?", ("2026-09-01", fut_id))
again = {e["title"]: e for e in _eng.evaluate_watchlist(t_wl, cap)}
check("becomes eligible once released", again.get("Future Drama", {}).get("eligible") is True)

# and the filter can be switched off entirely
_svc.set_integration("filter_unreleased", False)
off = {e["title"]: e for e in _eng.evaluate_watchlist(t_wl, cap)}
check("filter can be turned off", off.get("Future Drama") is not None)
_svc.set_integration("filter_unreleased", True)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)