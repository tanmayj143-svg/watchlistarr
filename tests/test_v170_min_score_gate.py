"""v1.7.0: automatic requests never go to zero-score filler films.

Restores the invariant from the v1.3.1 production incident ("it just requested
the top 5 films ... the latest that I added"): a film that matched NO rule
scores 0 and, while eligible, must never be auto-requested. Manual requests
(Request next / per-row Request) deliberately bypass this.

Run: python tests/test_v170_min_score_gate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["WATCHLISTARR_DATA"] = tempfile.mkdtemp(prefix="wlarr-v170-")
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


from app import db, engine, watchlists as wl_svc  # noqa: E402
from app.arr.seerr import SeerrRequestResult  # noqa: E402

db.init_db()

SENT: list[str] = []


class _FakeSeerr:
    def __init__(self, *a, **k):
        pass

    def create_request(self, *, title="", **k):
        SENT.append(title)
        return SeerrRequestResult(ok=True, request_id=len(SENT), status="pending",
                                  detail=title)


print("\n1. zero-score films are never auto-requested")
db.execute("DELETE FROM candidates")
db.execute("DELETE FROM watchlists")
wl = wl_svc.create_watchlist(
    name="v170 — no matching rules",
    source_type="letterboxd",
    source_ref={"list_url": "https://letterboxd.com/x/list/y/"},
    config={"max_per_run": 5, "rules": []},
    enabled=True,
)
now = db.now()
for i, title in enumerate(["Unwanted A", "Unwanted B", "Unwanted C"]):
    db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, status, tmdb_id,"
        " genres, countries, languages, meta, source_position, first_seen_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (wl["id"], f"z{i}", title, "discovered", str(100 + i), "[]", "[]", "[]",
         "{}", i, now, now),
    )
_wl = wl_svc.get_watchlist(wl["id"])

_orig_client = engine.SeerrClient
engine.SeerrClient = _FakeSeerr
_orig_int = engine._integration
engine._integration = lambda key, default=None: (
    "http://seerr.test" if key == "seerr_url" else ("k" if key == "seerr_api_key" else _orig_int(key, default))
)
try:
    res = engine.request_for_watchlist(wl_svc.get_watchlist(wl["id"]))
    check("cycle reported ok", res.get("ok") is True, str(res))
    check("nothing auto-requested from an all-zero queue",
          res.get("requested", 1) == 0, str(res.get("requested")))
    check("Seerr received no POST", len(SENT) == 0, str(SENT))
    row = db.one("SELECT status FROM candidates WHERE source_key='z0'")
    check("candidates stay pending, not errored", row["status"] == "discovered",
          row["status"])
finally:
    engine.SeerrClient = _orig_client
    engine._integration = _orig_int

print("\n2. a matching film IS auto-requested from the same queue")
db.execute(
    "UPDATE candidates SET year=2020 WHERE source_key='z0'", ()
)
db.execute(
    "UPDATE candidates SET year=1990 WHERE source_key IN ('z1','z2')", ()
)
_wl2 = wl_svc.get_watchlist(wl["id"])
_wl2["config"]["rules"] = [{"id": "dec", "kind": "decade", "op": "in",
                            "value": [2020], "points": 10}]
engine._integration = _orig_int
engine.SeerrClient = _FakeSeerr
engine._integration = lambda key, default=None: (
    "http://seerr.test" if key == "seerr_url" else ("k" if key == "seerr_api_key" else _orig_int(key, default))
)
try:
    res2 = engine.request_for_watchlist(_wl2)
    check("top match requested", res2.get("requested") == 1, str(res2))
    check("only the matching film went out",
          SENT == ["Unwanted A"], str(SENT))
finally:
    engine.SeerrClient = _orig_client
    engine._integration = _orig_int

print("\n3. manual Request Next bypasses the zero-score gate")
engine.SeerrClient = _FakeSeerr
engine._integration = lambda key, default=None: (
    "http://seerr.test" if key == "seerr_url" else ("k" if key == "seerr_api_key" else _orig_int(key, default))
)
try:
    res3 = engine.request_for_watchlist(wl_svc.get_watchlist(wl["id"]), manual=True)
    check("manual request went out despite 0 score",
          res3.get("requested") == 1, str(res3))
finally:
    engine.SeerrClient = _orig_client
    engine._integration = _orig_int

print("\n4. per-row manual request also bypasses the gate")
cid = db.one(
    "SELECT id FROM candidates WHERE watchlist_id=? AND status!='requested' LIMIT 1",
    (wl["id"],),
)["id"]
engine.SeerrClient = _FakeSeerr
engine._integration = lambda key, default=None: (
    "http://seerr.test" if key == "seerr_url" else ("k" if key == "seerr_api_key" else _orig_int(key, default))
)
try:
    res4 = engine.request_single_candidate(cid)
    check("row request ok", res4.get("ok") is True, str(res4))
finally:
    engine.SeerrClient = _orig_client
    engine._integration = _orig_int

print("\n5. refresh_scores(return_items=True) feeds request_from_items without a re-eval")
db.execute("DELETE FROM candidates")
db.execute("DELETE FROM watchlists")
wl2 = wl_svc.create_watchlist(
    name="v170 — pipeline",
    source_type="letterboxd",
    source_ref={"list_url": "https://letterboxd.com/x/list/y/"},
    config={"max_per_run": 1,
            "rules": [{"id": "dec", "kind": "decade", "op": "in", "value": [1999], "points": 5}]},
    enabled=True,
)
for i, title in enumerate(["Wanted One", "Wanted Two", "Filler"]):
    db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, status, tmdb_id,"
        " genres, countries, languages, meta, source_position, year, first_seen_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wl2["id"], f"p{i}", title, "discovered", str(200 + i), "[]", "[]", "[]",
         "{}", i, 1999 if i < 2 else 2021, now, now),
    )
with patch.object(engine, "evaluate_watchlist", wraps=engine.evaluate_watchlist) as spy:
    items = engine.refresh_scores(wl_svc.get_watchlist(wl2["id"]), return_items=True)
    calls_before = spy.call_count
    engine._integration = lambda key, default=None: (
        "http://seerr.test" if key == "seerr_url" else ("k" if key == "seerr_api_key" else _orig_int(key, default))
    )
    try:
        engine.SeerrClient = _FakeSeerr
        res5 = engine.request_for_watchlist_from_items(wl_svc.get_watchlist(wl2["id"]), items)
    finally:
        engine.SeerrClient = _orig_client
        engine._integration = _orig_int
    check("evaluate ran exactly once for score+request",
          spy.call_count == 1, f"called {spy.call_count}x")
    check("top item requested from pre-evaluated list",
          res5.get("requested") == 1, str(res5))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
