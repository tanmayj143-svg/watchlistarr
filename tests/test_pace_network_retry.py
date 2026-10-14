"""Test network error resilience and automatic unmet pace retry.

Run: python tests/test_pace_network_retry.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-retry-"))

from app import db, engine
from app.arr.seerr import SeerrRequestResult
from app.web.server import _should_retry_unmet_pace

PASS = FAIL = 0

def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


print("\n1. Network error halts batch without marking candidates as error")
db.init_db()

db.set_setting("integration.seerr_url", "http://mock-seerr:5055")
db.set_setting("integration.seerr_api_key", "mock-key")

db.execute("DELETE FROM candidates")
db.execute("DELETE FROM watchlists")
db.execute(
    "INSERT INTO watchlists (id, name, source_type, source_ref, config, enabled, sort_order, created_at, updated_at) "
    "VALUES (101, 'Test Watchlist', 'letterboxd', '{}', "
    "'{\"enabled\": true, \"quota_count\": 2, \"quota_window\": \"day\", \"max_per_run\": 5}', 1, 1, ?, ?)",
    (db.now(), db.now())
)
db.execute(
    "INSERT INTO candidates (id, watchlist_id, source_key, title, status, score, tmdb_id, released, year, first_seen_at, updated_at) "
    "VALUES (1, 101, 'k1', 'Movie A', 'discovered', 10.0, '101', '2020-01-01', 2020, ?, ?)",
    (db.now(), db.now())
)
db.execute(
    "INSERT INTO candidates (id, watchlist_id, source_key, title, status, score, tmdb_id, released, year, first_seen_at, updated_at) "
    "VALUES (2, 101, 'k2', 'Movie B', 'discovered', 9.0, '102', '2020-01-01', 2020, ?, ?)",
    (db.now(), db.now())
)

row = db.one("SELECT * FROM watchlists WHERE id=101")
assert row is not None
wl = db.row_to_watchlist(row)

# Mock SeerrClient to return network error
with patch("app.engine.SeerrClient") as mock_client_cls:
    client_instance = MagicMock()
    mock_client_cls.return_value = client_instance
    client_instance.create_request.return_value = SeerrRequestResult(
        ok=False, detail="Network error: [Errno 113] No route to host"
    )
    res = engine.request_for_watchlist(wl)

check("result reported not ok on network failure", not res["ok"], str(res))
check("create_request only called once (batch halted early)", client_instance.create_request.call_count == 1)

cand1 = db.one("SELECT * FROM candidates WHERE id=1")
cand2 = db.one("SELECT * FROM candidates WHERE id=2")
assert cand1 is not None and cand2 is not None
check("candidate 1 status preserved as discovered (not poisoned to error)", cand1["status"] == "discovered", cand1["status"])
check("candidate 2 never touched", cand2["status"] == "discovered", cand2["status"])

print("\n2. _should_retry_unmet_pace detects unmet quota with pending candidates")
check("unmet pace detected when quota is 2 and requests are 0", _should_retry_unmet_pace() is True)

# Mark 2 requests made
db.execute(
    "UPDATE candidates SET status='requested', requested_at=? WHERE id IN (1, 2)",
    (db.now(),)
)
check("unmet pace is False once quota of 2/day is reached", _should_retry_unmet_pace() is False)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
