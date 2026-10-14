"""v1.7.2: Pace becomes Frequency — an even-spread trigger, not a maximum.

Operator decision: "N per window" now means one request every
(window_length / N) days, waited out slot by slot. N=2/month means one film
every ~15 days: a slot is available when enough time has passed since the last
request within the current window. Once N requests have gone out in the window,
it waits for the next window — same ceiling as before, but the rate is spread
evenly instead of burst-then-block.

Run: python tests/test_v172_frequency.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-v172-"))
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


from app.scoring import Scorer, Rule, parse_rules  # noqa: E402
from app.timewin import resolve_window  # noqa: E402

NOW = datetime.now(timezone.utc)


def _cand(counts, last_at=None):
    return {
        "title": "X", "_window_counts": counts,
        "_window_last_requested_at": last_at.isoformat() if last_at else None,
    }


def _freq_rule(window="month", count=2):
    return Rule(id="freq", kind="frequency", window=window, threshold=float(count),
                limit=True, points=0, enabled=True)


print("\n1. slot arithmetic: N per window = one every window_length/N")
# 2/month -> 15-day slots
scorer = Scorer([_freq_rule("month", 2)])
check("no requests yet -> slot open",
      scorer.score(_cand({"month": 0}), now=NOW).eligible is True)
check("1 requested 20 days ago (slot 15d) -> next slot open",
      scorer.score(_cand({"month": 1}, NOW - timedelta(days=20)), now=NOW).eligible is True)
check("1 requested 5 days ago (slot 15d) -> waiting",
      scorer.score(_cand({"month": 1}, NOW - timedelta(days=5)), now=NOW).eligible is False)
check("quota full (2 this month) -> waiting for next window",
      scorer.score(_cand({"month": 2}, NOW - timedelta(days=20)), now=NOW).eligible is False)

print("\n2. 1/day = every day")
scorer = Scorer([_freq_rule("day", 1)])
check("requested 25h ago (outside today) -> day count 0, slot open",
      scorer.score(_cand({"day": 0}, NOW - timedelta(hours=25)), now=NOW).eligible is True)
check("requested 2h ago -> waiting",
      scorer.score(_cand({"day": 1}, NOW - timedelta(hours=2)), now=NOW).eligible is False)

print("\n3. 4/month -> 7.5-day slots")
scorer = Scorer([_freq_rule("month", 4)])
check("3 requested, last 8 days ago -> 4th slot open",
      scorer.score(_cand({"month": 3}, NOW - timedelta(days=8)), now=NOW).eligible is True)
check("3 requested, last 3 days ago -> waiting",
      scorer.score(_cand({"month": 3}, NOW - timedelta(days=3)), now=NOW).eligible is False)
check("4 requested (full) -> waiting",
      scorer.score(_cand({"month": 4}, NOW - timedelta(days=8)), now=NOW).eligible is False)

print("\n4. score is untouched by frequency (independence preserved)")
res = scorer.score({**_cand({"month": 1}, NOW - timedelta(days=1)),
                    "year": 2020}, now=NOW)
rules = parse_rules([{"id": "dec", "kind": "decade", "op": "in", "value": [2020], "points": 10}])
res2 = Scorer(rules + [_freq_rule("month", 4)]).score(
    {**_cand({"month": 1}, NOW - timedelta(days=1)), "year": 2020}, now=NOW)
check("waiting film keeps its rule score", res2.score == 10.0, str(res2.score))
check("rejection reason present", res2.rejected_by is not None and
      res2.rejected_by["rule"]["kind"] == "frequency", str(res2.rejected_by))

print("\n5. legacy 'pace' kind still parses and behaves as frequency")
res3 = Scorer([Rule(id="p", kind="pace", window="month", threshold=2,
                    limit=True, points=0, enabled=True)]).score(
    _cand({"month": 1}, NOW - timedelta(days=1)), now=NOW)
check("pace rule waits inside a slot like frequency", res3.eligible is False,
      str(res3.rejected_by and res3.rejected_by["reason"]))

print("\n6. no last-request timestamp -> never blocked below the count")
res4 = Scorer([_freq_rule("month", 2)]).score(_cand({"month": 1}, None), now=NOW)
check("missing timestamp falls back to slot-open", res4.eligible is True,
      str(res4.rejected_by and res4.rejected_by["reason"]))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)
