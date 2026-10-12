"""Scoring tests — these encode the rules from the original brief.

Run: python -m pytest tests/ -v      (or: python tests/test_scoring.py)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scoring import Scorer, parse_rules  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class FakeCapacity:
    available = True
    free_gb = 40.0
    total_gb = 1000.0
    used_gb = 960.0
    used_fraction = 0.96
    movie_count = 812


def cand(**kw):
    base = {
        "title": "x",
        "year": 2000,
        "genres": [],
        "countries": [],
        "languages": [],
        "imdb_rating": None,
        "popularity": None,
        "runtime": None,
        "source_position": 0,
        "_window_request_count": 0,
        "meta": {},
    }
    base.update(kw)
    return base


def test_1960s_china_month():
    """'Films from 1960s China this month' -> China 1960s outranks everything."""
    rules = parse_rules([
        {"id": "pace", "kind": "pace", "window": "month", "threshold": 4,
         "limit": True, "points": 0},
        {"id": "dec", "kind": "decade", "op": "in", "value": [1960], "points": 40},
        {"id": "ctry", "kind": "country", "op": "in", "value": ["China", "Hong Kong"], "points": 30},
        {"id": "lang", "kind": "language", "op": "in", "value": ["Mandarin"], "points": 20},
        {"id": "new", "kind": "decade", "op": "nin", "value": [1960], "points": -15},
    ])
    s = Scorer(rules)

    target = cand(title="Two Stage Sisters", year=1964, countries=["China"],
                  languages=["Mandarin"], genres=["Drama"])
    other_cn = cand(title="Modern CN", year=2019, countries=["China"], languages=["Mandarin"])
    other_decade = cand(title="Japan 1963", year=1963, countries=["Japan"], languages=["Japanese"])
    us_modern = cand(title="US 2021", year=2021, countries=["United States"], languages=["English"])

    st, o1, o2, o3 = (s.score(c, now=NOW) for c in (target, other_cn, other_decade, us_modern))
    assert st.score == 90, st.score
    # Additive semantics: the conjunctive match wins outright. A film matching
    # only ONE strong slider (1960s Japan = 40) can outrank one matching two
    # weaker ones (modern China = 30+20-15 = 35) -- that is the documented
    # behaviour of point-based scoring, and why the UI shows the breakdown.
    assert st.score > o2.score > o1.score > o3.score, (st.score, o1.score, o2.score, o3.score)
    assert o3.score == -15, o3.score
    print("ok  1960s China this month:", st.score, "1960s-other", o2.score,
          "modern-China", o1.score, "modern-US", o3.score)


def test_scifi_week_pace_blocks():
    """Pace of 2/week must reject everything once 2 are already requested."""
    rules = parse_rules([
        {"id": "pace", "kind": "pace", "window": "week", "threshold": 2,
         "limit": True, "points": 0},
        {"id": "sf", "kind": "genre", "op": "in", "value": ["Sci-Fi"], "points": 40},
    ])
    s = Scorer(rules)
    sf = cand(genres=["Sci-Fi"])

    under = s.score(cand(genres=["Sci-Fi"], _window_request_count=1), now=NOW)
    assert under.eligible and under.score == 40

    at_quota = s.score(cand(genres=["Sci-Fi"], _window_request_count=2), now=NOW)
    assert not at_quota.eligible, "pace must reject at the cap"
    assert at_quota.rejected_by and at_quota.rejected_by["rule"]["id"] == "pace"
    print("ok  Sci-Fi week pace blocks at 2:", under.score, "-> rejected")


def test_disk_space_gate():
    """Under 100 GB free -> hard block; above -> allowed."""
    rules = parse_rules([
        {"id": "disk", "kind": "capacity", "unit": "free_gb", "comparator": "lt",
         "threshold": 100, "limit": True, "points": 0},
        {"id": "rated", "kind": "rating", "op": "gte", "value": 7.5, "points": 25},
    ])
    low = Scorer(rules, capacity=FakeCapacity())
    r = low.score(cand(imdb_rating=8.5), now=NOW)
    assert not r.eligible, "40 GB free must block"

    class Roomy(FakeCapacity):
        free_gb = 400.0

    ok = Scorer(rules, capacity=Roomy()).score(cand(imdb_rating=8.5), now=NOW)
    assert ok.eligible and ok.score == 25, ok
    print("ok  disk gate blocks at 40GB, allows at 400GB:", r.rejected_by["reason"])


def test_genre_alias_and_negatives():
    """'science fiction' must match a Sci-Fi slider; penalties must apply."""
    rules = parse_rules([
        {"id": "sf", "kind": "genre", "op": "in", "value": ["Sci-Fi"], "points": 40},
        {"id": "nsf", "kind": "genre", "op": "nin", "value": ["Sci-Fi"], "points": -25},
    ])
    s = Scorer(rules)
    alias = s.score(cand(genres=["Science Fiction"]), now=NOW)
    assert alias.score == 40, alias.score
    drama = s.score(cand(genres=["Drama"]), now=NOW)
    assert drama.score == -25, drama.score
    print("ok  genre alias + negative points:", alias.score, drama.score)


def test_malformed_rule_is_skipped():
    rules = parse_rules([
        {"id": "ok", "kind": "genre", "op": "in", "value": ["Drama"], "points": 5},
        {"id": "bad", "kind": "not-a-kind", "nope": 1},
    ])
    assert any(r.id == "ok" for r in rules)
    s = Scorer(rules)
    assert s.score(cand(genres=["Drama"]), now=NOW).score == 5
    print("ok  malformed rule skipped without breaking scoring")


def test_explain_breakdown():
    rules = parse_rules([
        {"id": "sf", "kind": "genre", "op": "in", "value": ["Sci-Fi"], "points": 40},
        {"id": "hi", "kind": "rating", "op": "gte", "value": 8.0, "points": 20},
    ])
    r = Scorer(rules).score(cand(genres=["Sci-Fi", "Thriller"], imdb_rating=8.4), now=NOW)
    assert r.score == 60
    assert len(r.matched) == 2
    assert all("why" in m for m in r.matched)
    print("ok  explain breakdown:", [f"{m['rule_id']}+{m['points']}" for m in r.matched])


if __name__ == "__main__":
    for fn in [
        test_1960s_china_month,
        test_scifi_week_pace_blocks,
        test_disk_space_gate,
        test_genre_alias_and_negatives,
        test_malformed_rule_is_skipped,
        test_explain_breakdown,
    ]:
        fn()
    print("\nAll scoring tests passed.")