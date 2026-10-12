"""Ranking tests (v1.3.2).

Requirement from the operator: every film is scored and ordered by score, and
nothing is filtered out. An earlier attempt used a `min_score` gate so that
zero-score films would not be requested. That was rejected -- it hid films
instead of ranking them -- and has been removed.

These tests lock in:
  1. Every film is evaluated; none is discarded for scoring low.
  2. Ordering is by score, highest first.
  3. Ties break deterministically on rating, then on the user's list order.
  4. source_position is actually persisted, so list order is a real tie-breaker.
  5. Numeric rule values stored as strings still match.

Run: python tests/test_ranking.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-rank2-"))
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


from app.arr.radarr import RadarrCapacity  # noqa: E402
from app.scoring import Scorer, parse_rules  # noqa: E402


print("\n1. Numeric rule values saved as strings still match")
# Production stored "value": "4" (string) rather than 4.
s = Scorer(parse_rules([
    {"id": "lb", "kind": "letterboxd_rating", "op": "gte", "value": "4", "points": 20},
]))
check('string "4" matches 4.5', s.score({"letterboxd_rating": 4.5}, now=NOW).score == 20)
check('string "4" does not match 3.9', s.score({"letterboxd_rating": 3.9}, now=NOW).score == 0)
r_str45 = Scorer(parse_rules([
    {"kind": "letterboxd_rating", "op": "gte", "value": "4.5", "points": 10},
])).score({"letterboxd_rating": 4.6}, now=NOW).score
check('string "4.5" matches 4.6', r_str45 == 10)
r_num4 = Scorer(parse_rules([
    {"kind": "letterboxd_rating", "op": "gte", "value": 4, "points": 20},
])).score({"letterboxd_rating": 4.5}, now=NOW).score
check("numeric 4 unchanged", r_num4 == 20)

print("\n2. A blank numeric value must not match everything")
blank = Scorer(parse_rules([
    {"kind": "letterboxd_rating", "op": "gte", "value": "", "points": 20}]))
check('empty string does not match 0', blank.score({"letterboxd_rating": 0.0}, now=NOW).score == 0)
none_val = Scorer(parse_rules([
    {"kind": "letterboxd_rating", "op": "gte", "value": None, "points": 20}]))
check("None does not match 0", none_val.score({"letterboxd_rating": 0.0}, now=NOW).score == 0)


def cap() -> RadarrCapacity:
    c = RadarrCapacity()
    c.available = True
    c.free_bytes = 500 * 1024**3
    c.total_bytes = 1000 * 1024**3
    c.movie_count = 100
    return c


from app import db as _db  # noqa: E402

_db.init_db()
from app import engine as _eng  # noqa: E402
from app import watchlists as _wl  # noqa: E402

_wl.create_watchlist(
    name="ranking-v132", source_type="letterboxd",
    source_ref={"list_url": "https://letterboxd.com/x/list/y/"},
    config={
        "max_per_run": 5,
        "rules": [
            {"id": "g", "kind": "genre", "op": "in",
             "value": ["Sci-Fi", "Political"], "points": 10},
            {"id": "c", "kind": "country", "op": "in", "value": ["India"], "points": 10},
        ],
    },
)
t = _wl.list_watchlists()[-1]

# title, countries, genres, year, imdb_rating, lb_rating
FIXTURES = [
    ("India Drama",     ["India"], ["Drama"], 2024, 8.1, 4.2),   # genre+country = 20
    ("India Sci-Fi",    ["India"], ["Sci-Fi"], 2024, 7.5, 4.6),  # genre+country = 20
    ("China Drama",     ["China"], ["Drama"], 1999, 8.6, 4.4),   # 0
    ("USA Doc",         ["United States"], ["Documentary"], 2021, 7.9, 3.8),  # 0
    ("Japan Drama",     ["Japan"], ["Drama"], 2020, 8.2, 4.5),   # 0
    ("France Thriller", ["France"], ["Thriller"], 2019, 7.2, 4.0),  # 0
]
for i, (title, countries, genres, year, imdb, lb) in enumerate(FIXTURES):
    _db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, year, countries, genres,"
        " imdb_rating, lb_rating, meta, source_position, first_seen_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["id"], f"lb:{i}", title, year, _db.json.dumps(countries),
         _db.json.dumps(genres), imdb, lb, _db.json.dumps({}), i,
         _db.now(), _db.now()),
    )

evaluated = _eng.evaluate_watchlist(t, cap())

print("\n3. Every film is scored and ordered; nothing is filtered")
check("all films evaluated", len(evaluated) == len(FIXTURES),
      f"{len(evaluated)} of {len(FIXTURES)}")
check("no film blocked for a low score", all(e["eligible"] for e in evaluated),
      str([e["title"] for e in evaluated if not e["eligible"]]) or "all eligible")
check("every film carries a score",
      all(isinstance(e.get("score"), (int, float)) for e in evaluated))

print("\n4. Ordering is by score, highest first")
scores = [e["score"] for e in evaluated]
check("scores non-increasing", scores == sorted(scores, reverse=True), str(scores))
# Only "India Sci-Fi" matches BOTH rules (genre + country) = 20.
# "India Drama" matches the country rule only = 10.
check("highest scorer first", evaluated[0]["title"] == "India Sci-Fi",
      f'{evaluated[0]["title"]} = {evaluated[0]["score"]}')
check("the two-rule film scores 20", evaluated[0]["score"] == 20, str(evaluated[0]["score"]))
check("the country-only film is second at 10", evaluated[1]["title"] == "India Drama",
      f'{evaluated[1]["title"]} = {evaluated[1]["score"]}')
check("India Drama scores 10", evaluated[1]["score"] == 10, str(evaluated[1]["score"]))
check("next film scores 0", evaluated[2]["score"] == 0, str(evaluated[2]["score"]))

print("\n5. Ties break on rating, not insertion order")
zero = [e for e in evaluated if e["score"] == 0]
check("four zero-scored films", len(zero) == 4, str(len(zero)))
check("tied films ordered by Letterboxd rating, descending",
      [e["lb_rating"] for e in zero] == sorted((e["lb_rating"] for e in zero), reverse=True),
      str([(e["title"][:16], e["lb_rating"]) for e in zero]))
check("highest-rated tie immediately below the 20-point films",
      evaluated[2]["lb_rating"] == max(e["lb_rating"] for e in zero),
      f'{evaluated[2]["title"]} LB={evaluated[2]["lb_rating"]}')

print("\n6. source_position is persisted, so list order is a real tie-breaker")
rows = _db.query("SELECT id, title, source_position FROM candidates WHERE watchlist_id=?",
                 (t["id"],))
check("no NULL positions", all(r["source_position"] is not None for r in rows))
check("positions are 0..n-1", sorted(r["source_position"] for r in rows) == list(range(len(rows))),
      str(sorted(r["source_position"] for r in rows)))

for r in rows:  # strip ratings so only list position can decide
    _db.execute(
        "UPDATE candidates SET lb_rating=NULL, imdb_rating=NULL, popularity=NULL WHERE id=?",
        (r["id"],),
    )
zeros = [e for e in _eng.evaluate_watchlist(t, cap()) if e["score"] == 0]
check("with ratings equal, source_position decides",
      [e["source_position"] for e in zeros] == sorted(e["source_position"] for e in zeros),
      str([(e["title"][:16], e["source_position"]) for e in zeros]))

print("\n7. Films with no metadata score 0 but are still ranked")
blank_cand = {"title": "Unknown", "year": None, "genres": [], "countries": [],
              "languages": [], "_window_counts": {}, "_window_request_count": 0}
res = Scorer(parse_rules([
    {"kind": "country", "op": "in", "value": ["India"], "points": 10}])).score(blank_cand, now=NOW)
check("no data -> 0 points", res.score == 0, str(res.score))
check("and reports nothing matched", res.matched == [], str(res.matched))

print("\n8. The removed min_score setting is ignored, not honoured")
t2 = _wl.get_watchlist(t["id"])
t2["config"]["min_score"] = 99   # would have blocked everything before
still = _eng.evaluate_watchlist(t2, cap())
check("a stale min_score value does not filter films",
      len(still) == len(FIXTURES) and all(e["eligible"] for e in still),
      f"{len(still)} evaluated, all eligible")

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)