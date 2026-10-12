"""Scoring-independence tests (v1.3.8).

Operator requirements:
  1. Scoring must be computed regardless of Pace / storage gates. Those gates are
     an *intake valve* -- they decide whether a request may be sent now, never
     what a film is worth.
  2. A higher score always ranks first, including among films currently gated.
  3. The digital-release column must be populated for every film, not "?".

The regression: `refresh_scores()` wrote `0.0` for any film that was ineligible,
so a film paused by Pace looked identical to a film nobody wanted. With Pace set
to 3/week and 5 already requested, all 800 pending films were stored as 0.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-score-"))
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
from app import db as _db  # noqa: E402

_db.init_db()
from app import engine as _eng  # noqa: E402
from app import settings_service as _svc  # noqa: E402
from app import watchlists as _wl  # noqa: E402


def cap() -> RadarrCapacity:
    c = RadarrCapacity()
    c.available = True
    c.free_bytes = 500 * 1024**3
    c.total_bytes = 1000 * 1024**3
    c.movie_count = 100
    return c


_svc.set_integration("filter_unreleased", False)   # isolate the scoring behaviour
_svc.set_integration("ignore_radarr_existing", False)
_svc.set_integration("tmdb_api_key", "")

_wl.create_watchlist(
    name="score-independence", source_type="letterboxd",
    source_ref={"list_url": "https://example.test/x/"},
    config={
        "max_per_run": 1,
        # Pace already exhausted: 1 of 1 per week used.
        "quota_count": 1,
        "quota_window": "week",
        "rules": [
            {"id": "lb", "kind": "letterboxd_rating", "op": "gte", "value": 4, "points": 20},
            {"id": "ctry", "kind": "country", "op": "in", "value": ["India"], "points": 10},
        ],
    },
)
t = _wl.list_watchlists()[-1]

# Pretend the weekly quota is already spent, which gates everything.
import app.engine as _eng_mod  # noqa: E402

_real_counts = _eng._window_counts
_eng._window_counts = lambda wl_id, now=None: {
    "day": 1, "week": 1, "month": 1, "quarter": 1, "year": 1,
}

FIX = [
    ("Best Film",    ["India"], 4.8),   # 30 pts
    ("Good Film",    ["France"], 4.5),  # 20 pts
    ("Plain Film",   ["Japan"], 3.0),   # 0 pts
]
for i, (title, countries, lb) in enumerate(FIX):
    _db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, countries, lb_rating,"
        " imdb_rating, meta, source_position, first_seen_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (t["id"], f"lb:{i}", title, _db.json.dumps(countries), lb, 7.0,
         _db.json.dumps({}), i, _db.now(), _db.now()),
    )

print("\n1. Scoring is computed even when the intake valve is closed")
wl = _wl.get_watchlist(t["id"])
scored = _eng.evaluate_watchlist(wl, cap())
by_title = {e["title"]: e for e in scored}

check("every film still has a score", len(scored) == 3, str(len(scored)))
check("30-point film scored 30", by_title["Best Film"]["score"] == 30,
      str(by_title["Best Film"]["score"]))
check("20-point film scored 20", by_title["Good Film"]["score"] == 20,
      str(by_title["Good Film"]["score"]))
check("0-point film scored 0", by_title["Plain Film"]["score"] == 0)
check("Pace gate is what blocks them, not the score",
      all(e["rejected_by"] and e["rejected_by"]["rule"]["kind"] == "pace"
          for e in scored),
      str([e["rejected_by"]["rule"]["kind"] if e["rejected_by"] else None for e in scored]))

print("\n2. Persisted scores are NOT zeroed by a gate")
_eng.refresh_scores(wl, cap())
rows = {r["title"]: r["score"] for r in
        _db.query("SELECT title, score FROM candidates WHERE watchlist_id=?", (t["id"],))}
check("stored score kept for a gated 30-point film", rows.get("Best Film") == 30,
      str(rows.get("Best Film")))
check("stored score kept for a gated 20-point film", rows.get("Good Film") == 20,
      str(rows.get("Good Film")))
check("only the genuinely-unwanted film is 0", rows.get("Plain Film") == 0,
      str(rows.get("Plain Film")))

print("\n3. Ranking is by score alone, highest first")
order = [e["title"] for e in scored]
check("order is Best, Good, Plain", order == ["Best Film", "Good Film", "Plain Film"],
      str(order))
scores = [e["score"] for e in scored]
check("scores strictly descending", scores == sorted(scores, reverse=True), str(scores))

print("\n4. A high-scoring film still outranks a low one even when both are gated")
wl2 = _wl.get_watchlist(t["id"])
again = _eng.evaluate_watchlist(wl2, cap())
check("30-point film ranks first despite being gated",
      again[0]["title"] == "Best Film" and again[0]["score"] == 30,
      f'{again[0]["title"]}={again[0]["score"]}')

print("\n5. Digital-release verdict is stored for every film")
_svc.set_integration("filter_unreleased", True)
wl3 = _wl.get_watchlist(t["id"])
_eng.refresh_scores(wl3, cap())
metas = {r["title"]: _db.loads(r["meta"], {}) for r in
         _db.query("SELECT title, meta FROM candidates WHERE watchlist_id=?", (t["id"],))}
for title in ("Best Film", "Good Film", "Plain Film"):
    rel = metas[title].get("release")
    check(f"{title} has a stored verdict", isinstance(rel, dict),
          str(rel)[:70] if rel else "MISSING")
    if isinstance(rel, dict):
        check(f"{title} verdict has available + note",
              "available" in rel and "note" in rel, str(rel)[:70])

print("\n6. Scoring does not depend on the release gate either")
# With the filter on, a not-yet-released film must still carry its real score.
_unreleased = _db.insert(
    "INSERT INTO candidates(watchlist_id, source_key, title, countries, lb_rating,"
    " year, released, imdb_rating, meta, source_position, first_seen_at, updated_at)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
    (t["id"], "lb:future", "Future Film", _db.json.dumps(["India"]), 4.9, 2028,
     "2028-06-01", 7.5, _db.json.dumps({}), 9, _db.now(), _db.now()),
)
wl4 = _wl.get_watchlist(t["id"])
_eng.refresh_scores(wl4, cap())
row = _db.one("SELECT score, meta FROM candidates WHERE id=?", (_unreleased,))
check("unreleased but highly-rated film keeps its score", row["score"] == 30,
      str(row["score"]))
_meta = _db.loads(row["meta"], {})
_reason = ((_meta.get("release") or {}).get("note") or "")
check("and carries a digital-release verdict",
      isinstance(_meta.get("release"), dict), str(_meta.get("release"))[:80])
check("verdict explains the hold", bool(_reason), _reason[:70])

_eng._window_counts = _real_counts

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)