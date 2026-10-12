"""v1.1.0 tests: storage guards, Radarr-exists skipping, expanded genres.

Run: python tests/test_v11.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-v11-"))
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.arr.radarr import RadarrCapacity, _norm_title  # noqa: E402
from app.scoring import (  # noqa: E402
    GENRES,
    Scorer,
    canonical_genre,
    parse_rules,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def cap(free_gb: float, total_gb: float = 2000.0, movies: int = 500) -> RadarrCapacity:
    c = RadarrCapacity()
    c.available = True
    c.free_bytes = int(free_gb * 1024**3)
    c.total_bytes = int(total_gb * 1024**3)
    c.movie_count = movies
    return c


# ---------------------------------------------------------------------------
print("\n1. Radarr library recognition")
c = cap(500)
c.tmdb_ids = {278, 240}
c.imdb_ids = {"tt0068646"}
c.titles = {"godfather", "godfather part ii", "shawshank redemption"}
c.years = {"godfather": 1972, "godfather part ii": 1974, "shawshank redemption": 1994}
c.with_file = {"godfather"}
# Only Godfather actually has a file. 240 / tt0068646 are tracked-but-missing,
# which since v1.6.0 means "you do not have this", not "already handled".
c.tmdb_with_file = {278}
c.tracked_missing = {240}
c.imdb_to_tmdb = {"tt0068646": 240}

check("matches on TMDB id when a file exists", c.knows(tmdb_id=278)[0])
check("tracked-but-missing is NOT a match on TMDB id", not c.knows(tmdb_id=240)[0])
check("tracked-but-missing is reported as missing instead",
      c.tracked_but_missing(tmdb_id=240) is not None)
check("tracked-but-missing is not a match on IMDb id", not c.knows(imdb_id="tt0068646")[0])
check("matches on title+year", c.knows(title="The Godfather", year=1972)[0])
check("normalises articles ('The X' == 'X, The')",
      _norm_title("The Godfather") == _norm_title("Godfather, The"))
check("unknown film not matched", not c.knows(tmdb_id=999999, title="Some New Film", year=2024)[0])
check("remake not confused by title alone",
      not c.knows(title="The Godfather", year=1990)[0], "different year must not match")

# deleted-file case
c2 = cap(500)
c2.titles = {"some deleted film"}
c2.years = {"some deleted film": 1999}
c2.with_file = set()  # record exists, file gone
# Superseded: a Radarr record whose file was deleted used to count as "already
# have it". Since v1.6.0 only a film with a file counts, so a deleted film is
# eligible again and is reported as tracked-but-missing.
known, why = c2.knows(title="Some Deleted Film", year=1999)
check("deleted film is not treated as existing", not known, str(why))
check("...but it is still tracked in Radarr",
      c2.tracked_but_missing(title="Some Deleted Film", year=1999) is not None)


# ---------------------------------------------------------------------------
print("\n2. Global storage guards")
from app.engine import _apply_storage_guards, _global_storage_guards  # noqa: E402

# _global_storage_guards reads from settings; test the pure applier directly.
check("no guards -> nothing blocked", _apply_storage_guards({}, cap(50)) is None)

g = {"min_free_gb": 100.0, "max_free_gb": None, "est_movie_gb": None}
check("min_free blocks below floor",
      "storage floor" in (_apply_storage_guards(g, cap(50)) or ""),
      str(_apply_storage_guards(g, cap(50))))
check("min_free allows at/above floor", _apply_storage_guards(g, cap(150)) is None)

g2 = {"min_free_gb": None, "max_free_gb": 500.0, "est_movie_gb": None}
check("max_free blocks above ceiling",
      "ceiling" in (_apply_storage_guards(g2, cap(900)) or ""),
      str(_apply_storage_guards(g2, cap(900))))
check("max_free allows at/below ceiling", _apply_storage_guards(g2, cap(400)) is None)

both = {"min_free_gb": 100.0, "max_free_gb": 500.0, "est_movie_gb": None}
check("floor wins when both would trigger", "storage floor" in (_apply_storage_guards(both, cap(40)) or ""))
check("unknown capacity never blocks (Radarr down)",
      _apply_storage_guards(g, RadarrCapacity(available=False)) is None)


# ---------------------------------------------------------------------------
print("\n3. min_free_gb / max_free_gb as rule kinds")
s = Scorer(parse_rules([
    {"id": "floor", "kind": "min_free_gb", "comparator": "gte", "threshold": 100,
     "limit": True, "points": 0},
]), capacity=cap(60))
r = s.score({"title": "x"}, now=NOW)
check("min_free_gb rule blocks under floor", not r.eligible, str(r.rejected_by))

s_ok = Scorer(parse_rules([
    {"id": "floor", "kind": "min_free_gb", "comparator": "gte", "threshold": 100,
     "limit": True, "points": 0},
]), capacity=cap(400))
check("min_free_gb rule allows above floor", s_ok.score({"title": "x"}, now=NOW).eligible)

s_max = Scorer(parse_rules([
    {"id": "ceil", "kind": "max_free_gb", "comparator": "lte", "threshold": 500,
     "limit": True, "points": 0},
]), capacity=cap(800))
r2 = s_max.score({"title": "x"}, now=NOW)
check("max_free_gb rule blocks above ceiling", not r2.eligible, str(r2.rejected_by))

# look-ahead: 502 GB free, ceiling 500, film 4 GB -> taking it would leave
# 498 (fine); at 503 free it would leave 499... still fine. The guard fires when
# free space is AT/BELOW the ceiling and the film would refill past it.
s_look = Scorer(parse_rules([
    {"id": "ceil", "kind": "max_free_gb", "comparator": "lte", "threshold": 500,
     "threshold2": 4, "limit": True, "points": 0},
]), capacity=cap(500))
r3 = s_look.score({"title": "x"}, now=NOW)
check("ceiling look-ahead lets a film that lands under the ceiling", r3.eligible,
      str(r3.rejected_by))

s_look2 = Scorer(parse_rules([
    {"id": "ceil", "kind": "max_free_gb", "comparator": "lte", "threshold": 500,
     "threshold2": 40, "limit": True, "points": 0},
]), capacity=cap(510))
r4 = s_look2.score({"title": "x"}, now=NOW)
check("ceiling look-ahead holds off a film that would overshoot", not r4.eligible,
      str(r4.rejected_by))

s_nofit = Scorer(parse_rules([
    {"id": "ceil", "kind": "max_free_gb", "comparator": "lte", "threshold": 500,
     "threshold2": 40, "limit": True, "points": 0},
]), capacity=cap(10))
r5 = s_nofit.score({"title": "x"}, now=NOW)
check("ceiling blocks when the film cannot fit at all", not r5.eligible, str(r5.rejected_by))


# ---------------------------------------------------------------------------
print("\n4. Expanded genre vocabulary")
check("genre list expanded well past 20", len(GENRES) >= 100, f"{len(GENRES)} genres")
check("no duplicate genres", len(GENRES) == len(set(GENRES)),
      str(len(GENRES) - len(set(GENRES))))
check("Science Fiction still canonicalises", canonical_genre("Science Fiction") == "Sci-Fi")
check("anime alias works", canonical_genre("anime") == "Japanese Anime")
check("no alias maps to empty string", canonical_genre("Live Action") != "")

sr = Scorer(parse_rules([
    {"id": "g", "kind": "genre", "op": "in",
     "value": ["Zombie", "Cyberpunk", "Heist", "Road Movie"], "points": 30},
]))
check("new niche genres score", sr.score({"genres": ["Zombie"]}, now=NOW).score == 30)


# ---------------------------------------------------------------------------
print("\n5. Pace counts per window (v1.1.3: kind renamed quota -> pace)")
# A "2 per week" rule must count this WEEK's requests, not the month total.
w_counts = {"day": 0, "week": 2, "month": 7, "quarter": 9, "year": 20}
base = {"title": "x", "_window_request_count": 7, "_window_counts": w_counts}

wk = Scorer(parse_rules([
    {"id": "pace", "kind": "pace", "window": "week", "threshold": 2,
     "limit": True, "points": 0},
]))
r = wk.score(dict(base), now=NOW)
check("weekly pace uses the WEEK count (2 >= 2 blocks)", not r.eligible, str(r.rejected_by["reason"]))

mo = Scorer(parse_rules([
    {"id": "pace", "kind": "pace", "window": "month", "threshold": 8,
     "limit": True, "points": 0},
]))
r2 = mo.score(dict(base), now=NOW)
check("monthly pace uses the MONTH count (7 < 8 allows)", r2.eligible)

yr = Scorer(parse_rules([
    {"id": "pace", "kind": "pace", "window": "year", "threshold": 25,
     "limit": True, "points": 0},
]))
check("yearly pace uses the YEAR count (20 < 25 allows)", yr.score(dict(base), now=NOW).eligible)

under = Scorer(parse_rules([
    {"id": "pace", "kind": "pace", "window": "week", "threshold": 3,
     "limit": True, "points": 0},
]))
check("weekly pace under the cap allows", under.score(dict(base), now=NOW).eligible)

# Legacy shape (no _window_counts) must still work via the fallback.
legacy = {"title": "x", "_window_request_count": 5}
r3 = Scorer(parse_rules([
    {"id": "pace", "kind": "pace", "window": "month", "threshold": 5,
     "limit": True, "points": 0},
])).score(legacy, now=NOW)
check("fallback to _window_request_count still works", not r3.eligible)


# ---------------------------------------------------------------------------
print("\n6. Skipped films are never requested")
import tempfile as _tf

os.environ["WATCHLISTARR_DATA"] = _tf.mkdtemp(prefix="wlarr-skip-")
from app import db as _db  # noqa: E402
from app import engine as _eng  # noqa: E402
from app import settings_service as _svc  # noqa: E402
from app import watchlists as _wl  # noqa: E402

_db.init_db()
_wl.create_watchlist(
    name="skip-test", source_type="letterboxd",
    source_ref={"list_url": "https://letterboxd.com/x/list/y/"},
    config={"max_per_run": 1, "rules": [
        {"id": "g", "kind": "genre", "op": "in", "value": ["Drama"], "points": 50},
    ]},
)
sk_wl = _wl.list_watchlists()[0]
sid = _db.insert(
    "INSERT INTO candidates(watchlist_id, source_key, title, year, genres, meta, "
    "source_position, first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
    (sk_wl["id"], "lb:test", "Skipped Drama", 2000,
     _db.json.dumps(["Drama"]),
     _db.json.dumps({"ignored": True, "ignore_reason": "not now"}),
     0, _db.now(), _db.now()),
)
keep_id = _db.insert(
    "INSERT INTO candidates(watchlist_id, source_key, title, year, genres, meta, "
    "source_position, first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
    (sk_wl["id"], "lb:test2", "Wanted Drama", 2001,
     _db.json.dumps(["Drama"]), _db.json.dumps({}), 1, _db.now(), _db.now()),
)

evaluated = _eng.evaluate_watchlist(sk_wl, cap(500))
by_title = {e["title"]: e for e in evaluated}
check("skipped film present in evaluation", "Skipped Drama" in by_title)
check("skipped film is ineligible", by_title["Skipped Drama"]["eligible"] is False)
check("skipped film reason is readable",
      "skipped" in (by_title["Skipped Drama"]["rejected_by"]["reason"]).lower(),
      by_title["Skipped Drama"]["rejected_by"]["reason"])
# Superseded: skipping used to force a film to rank last. Ranking is now by
# score alone -- skipping is an intake gate, not a ranking signal.
check("skipped film keeps its own score", by_title["Skipped Drama"]["score"] == 50,
      str(by_title["Skipped Drama"]["score"]))
check("ordering follows score, not skip state",
      [e["title"] for e in evaluated] == ["Skipped Drama", "Wanted Drama"],
      "equal scores -> stable order, neither is force-ranked last")
check("wanted film still eligible", by_title["Wanted Drama"]["eligible"] is True)

# unignore restores it
_svc.set_integration("seerr_url", "")
row = _db.one("SELECT meta FROM candidates WHERE id=?", (sid,))
m = _db.loads(row["meta"], {})
m["ignored"] = False
_db.execute("UPDATE candidates SET meta=? WHERE id=?", (_db.json.dumps(m), sid))
again = {e["title"]: e for e in _eng.evaluate_watchlist(sk_wl, cap(500))}
check("un-skipping makes the film eligible again", again["Skipped Drama"]["eligible"] is True)


# ---------------------------------------------------------------------------
print("\n7. Letterboxd rating rule + comparator persistence")
from app.scoring import Rule as _R  # noqa: E402

lb = Scorer(parse_rules([
    {"id": "lb", "kind": "letterboxd_rating", "op": "gte", "value": 4.0, "points": 25},
]))
check("letterboxd_rating scores above threshold",
      lb.score({"letterboxd_rating": 4.5}, now=NOW).score == 25)
check("letterboxd_rating excludes below threshold",
      lb.score({"letterboxd_rating": 3.2}, now=NOW).score == 0)
check("letterboxd_rating ignores missing value",
      lb.score({}, now=NOW).score == 0)
check("letterboxd_rating accepts lb_rating alias",
      lb.score({"lb_rating": 4.9}, now=NOW).score == 25)
# 0-5 scale, not 0-10: 4.0 of 5 is high, 4.0 of 10 is not
check("scale is 0-5 (4.0 counts as high)", lb.score({"letterboxd_rating": 4.0}, now=NOW).score == 25)

print("\n8. Limit-rule comparator is honoured (not dropped)")
for kind, extra in (("capacity", {"unit": "free_gb"}), ("min_free_gb", {}), ("max_free_gb", {})):
    for op in ("gte", "gt", "lte", "lt"):
        r = _R(id="x", kind=kind, comparator=op, threshold=100, limit=True, points=0, **extra)
        s2 = Scorer([r])
        # introspect: the rule must survive with its comparator intact
        check(f"{kind} comparator {op} preserved", s2.rules[0].comparator == op)

# capacity with < and > must actually differ in outcome
below = Scorer(parse_rules([
    {"id": "c", "kind": "capacity", "unit": "free_gb", "comparator": "lt",
     "threshold": 100, "limit": True, "points": 0},
]), capacity=cap(500))
above = Scorer(parse_rules([
    {"id": "c", "kind": "capacity", "unit": "free_gb", "comparator": "gt",
     "threshold": 100, "limit": True, "points": 0},
]), capacity=cap(500))
check("capacity 'lt 100' allows 500 GB", below.score({"title": "x"}, now=NOW).eligible)
check("capacity 'gt 100' blocks at 500 GB", not above.score({"title": "x"}, now=NOW).eligible)


# ---------------------------------------------------------------------------
print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)