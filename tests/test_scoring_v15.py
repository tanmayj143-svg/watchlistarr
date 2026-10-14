"""v1.5.0 tests: multi-match scoring, canonical Why column, source standardisation.

1. A film matching several values of one rule scores per value, capped.
2. The Why column is produced by app.explain in one format, not by templates.
3. Metadata has one authoritative source per field, never an appended mix.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-v15-"))
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


from app.scoring import Scorer, parse_rules  # noqa: E402

print("\n1. A film matching 2 genres scores twice")
rules = parse_rules([{"id": "g", "kind": "genre", "op": "in",
                      "value": ["Drama", "Sci-Fi"], "points": 10}])
two = Scorer(rules).score({"title": "A", "genres": ["Drama", "Sci-Fi"]})
one = Scorer(rules).score({"title": "B", "genres": ["Drama"]})
check("two matching genres -> 2 x points", two.score == 20, str(two.score))
check("one matching genre -> 1 x points", one.score == 10, str(one.score))

print("\n2. Matches are capped so one heavily-tagged film cannot dominate")
rules6 = parse_rules([{"id": "g6", "kind": "genre", "op": "in",
                       "value": ["a", "b", "c", "d", "e", "f"], "points": 10,
                       "max_matches": 3}])
six = Scorer(rules6).score({"title": "C", "genres": ["a", "b", "c", "d", "e", "f"]})
check("6 matching values capped at 3 -> 30", six.score == 30, str(six.score))
check("cap reported in the breakdown", len(six.matched) == 1, str(len(six.matched)))

print("\n3. Default cap is 3")
rules_d = parse_rules([{"id": "gd", "kind": "genre", "op": "in",
                        "value": ["a", "b", "c", "d", "e"], "points": 10}])
dflt = Scorer(rules_d).score({"title": "D", "genres": ["a", "b", "c", "d", "e"]})
check("default cap limits to 3 matches", dflt.score == 30, str(dflt.score))

print("\n4. Countries and languages behave the same way")
rc = parse_rules([{"id": "c", "kind": "country", "op": "in",
                   "value": ["India", "France"], "points": 15}])
sc2 = Scorer(rc).score({"title": "E", "countries": ["India", "France"]})
check("two countries -> 2 x points", sc2.score == 30, str(sc2.score))

print("\n5. Negative/exclusion rules are NOT multiplied")
rn = parse_rules([{"id": "n", "kind": "genre", "op": "nin",
                   "value": ["Horror"], "points": 10}])
neg = Scorer(rn).score({"title": "F", "genres": ["Drama", "Comedy"]})
check("nin awards points once", neg.score == 10, str(neg.score))

print("\n6. Two rules stack additively")
multi = parse_rules([
    {"id": "g", "kind": "genre", "op": "in", "value": ["Drama", "Sci-Fi"], "points": 10},
    {"id": "c", "kind": "country", "op": "in", "value": ["India"], "points": 20},
])
stacked = Scorer(multi).score({"title": "G", "genres": ["Drama", "Sci-Fi"],
                               "countries": ["India"]})
check("genres 20 + country 20 = 40", stacked.score == 40, str(stacked.score))

print("\n7. The Why column is canonical and app-generated")
from app.explain import explain, summary, rule_label  # noqa: E402

cand = {
    "title": "Resurrection",
    "breakdown": {
        "matched": [
            {"rule_id": "lb", "kind": "letterboxd_rating", "why": "Letterboxd rating 4.07 >= 4",
             "points": 20},
            {"rule_id": "g", "kind": "genre", "why": "genre Sci-Fi matches Drama, Sci-Fi (+10 for 1)",
             "points": 10},
        ],
        "rejected_by": None,
    },
}
lines = explain(cand)
check("one line per matched rule", len(lines) == 2, str(len(lines)))
check("highest contribution first", lines[0]["points"] == "+20", str(lines[0]))
check("uses a stable label, not a raw kind",
      lines[0]["label"] == "Letterboxd rating", lines[0]["label"])
check("summary is one line", " | " in summary(cand), summary(cand)[:60])

print("\n8. Explain handles the empty and blocked cases explicitly")
empty = explain({"title": "X", "breakdown": {"matched": [], "rejected_by": None}})
check("no rules matched is stated", empty[0]["label"] == "No rules matched",
      empty[0]["label"])
check("and it is never blank", bool(empty[0]["detail"]))

blocked_cand = {
    "title": "Y",
    "breakdown": {
        "matched": [],
        "rejected_by": {"rule": {"kind": "pace"},
                        "reason": "Pace limit: 5 of 3 this week"},
    },
}
blocked = explain(blocked_cand)
_status_line = __import__("app.explain", fromlist=["status_line"]).status_line
# Superseded: these asserted the gate reason appeared in Why. The Why column is
# for scoring only -- gates live in Status (app.explain.status_line).
check("Why stays scoring-only, so it says no rules matched",
      blocked[0]["label"] == "No rules matched", blocked[0]["label"])
check("the Pace gate is reported by status_line instead",
      "Pace" in _status_line(blocked_cand), _status_line(blocked_cand))

print("\n9. Explanation output is deterministic")
a = explain(cand)
b = explain(dict(cand, breakdown={"matched": list(reversed(cand["breakdown"]["matched"]))}))
check("same order regardless of input order",
      [x["label"] for x in a] == [x["label"] for x in b],
      str([x["label"] for x in a]))

print("\n10. Metadata has one authoritative source per field")
from app.metadata import providers as P  # noqa: E402

DETAIL = {
    "id": 999, "title": "Singular Source", "release_date": "2021-05-01",
    "imdb_id": "tt9999999", "runtime": 100, "vote_average": 7.0, "vote_count": 10,
    "genres": [{"id": 18, "name": "Drama"}],
    "production_countries": [{"iso_3166_1": "US", "name": "United States of America"}],
    "original_language": "en",
    "spoken_languages": [{"iso_639_1": "en", "name": "English"}],
}


class _R:
    def __init__(self, code, body):
        self.status_code = code
        self._b = body
        self.headers = {}

    def json(self):
        return self._b


class _C:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if "/search/movie" in url:
            return _R(200, {"results": [{"id": 999, "title": "Singular Source",
                                         "release_date": "2021-05-01",
                                         "genre_ids": [18]}]})
        return _R(200, DETAIL)


_orig = P._client
P._client = lambda: _C()
P._cache = P._TTLCache(1)
try:
    m = P.resolve(title="Singular Source", year=2021, slug="singular",
                  tmdb_key="key")
finally:
    P._client = _orig

check("resolved", m is not None)
if m is not None:
    check("provider recorded as tmdb", m.provider.startswith("tmdb"), m.provider)
    check("genres exactly the TMDB values", m.genres == ["Drama"], str(m.genres))
    check("country normalised, not duplicated",
          m.countries == ["United States"], str(m.countries))
    check("no wikipedia-style country appended",
          not any("America" in c for c in m.countries), str(m.countries))
    check("languages not duplicated", len(m.languages) == len(set(m.languages)),
          str(m.languages))
    check("language not guessed when real data exists",
          m.meta_language_guessed is False, str(m.meta_language_guessed))

print("\n11. TMDB search refuses an unrelated title")
class _C2(_C):
    def get(self, url, params=None):
        if "/search/movie" in url:
            return _R(200, {"results": [
                {"id": 1, "title": "The Bone Collector"},
                {"id": 2, "title": "Bone Tomahawk"},
            ]})
        return _R(200, DETAIL)


P._client = lambda: _C2()
P._cache = P._TTLCache(1)
try:
    bad = P.tmdb_find("Kaul", 2016, "key")
finally:
    P._client = _orig
check("unrelated results are rejected", bad is None, str(bad))

print("\n12. Country->language guess is REMOVED (v1.7.0: TMDB-only, no guesses)")
# The country-derived language heuristic was deleted with the Cinemeta-era
# fallbacks. Language now comes only from TMDB's original_language /
# spoken_languages fields.
check("guess helper is gone",
      not hasattr(P, "_language_from_countries"),
      "removed by design")
check("guess table is gone",
      not hasattr(P, "_COUNTRY_DEFAULT_LANGUAGE"),
      "removed by design")


print("\n13. Canonical duplicates are collapsed, not double-counted")
# "Science Fiction" and "Sci-Fi" both canonicalise to sci-fi. A film tagged
# "Science Fiction" against a rule asking for "Sci-Fi" is ONE match, not two.
dup_rules = parse_rules([{"id": "gd", "kind": "genre", "op": "in",
                          "value": ["Sci-Fi"], "points": 10}])
dup = Scorer(dup_rules).score({"title": "H", "genres": ["Science Fiction"]})
check("one canonical match from an alias", dup.score == 10, str(dup.score))
check("explanation does not repeat the genre",
      dup.matched and "sci-fi, sci-fi" not in dup.matched[0]["why"],
      dup.matched[0]["why"] if dup.matched else "no match")

dup2 = Scorer(dup_rules).score({"title": "I", "genres": ["Sci-Fi", "Science Fiction"]})
check("aliases on the film itself collapse to one", dup2.score == 10, str(dup2.score))

mixed = parse_rules([{"id": "gm", "kind": "genre", "op": "in",
                      "value": ["Sci-Fi", "Science Fiction", "Drama"], "points": 10}])
m3 = Scorer(mixed).score({"title": "J", "genres": ["Science Fiction", "Drama"]})
check("two genuinely distinct genres still count twice", m3.score == 20, str(m3.score))

print("\n14. Why text is free of doubled values")
from app.explain import explain as _ex  # noqa: E402
cand2 = {"title": "K", "breakdown": {"matched": dup.matched, "rejected_by": None}}
txt = _ex(cand2)[0]["detail"]
check("no repeated comma-separated value", ", sci-fi" not in txt, txt)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)