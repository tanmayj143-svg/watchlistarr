"""TMDB enrichment regression tests (v1.4.1).

Root cause of "my rules match almost nothing": TMDB's /search/movie returns only
numeric `genre_ids` -- never genre/country/language *names*. The app read
`results[0]["genres"]`, which is always absent, so every film stored an empty
genre list even with a working API key. Only the /movie/<id> details endpoint
carries names, so tmdb_find() must follow the search with a details call.

Second regression: TMDB reports official country names ("United States of
America"), while saved rules and the picker use short forms ("United States"), so
a saved rule silently matched zero films.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-tmdb-"))
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


from app.metadata import providers as P  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"

SEARCH = {
    "results": [{
        "id": 1233413, "title": "Sinners", "release_date": "2025-04-16",
        "genre_ids": [27, 28, 53], "overview": "o",
        "poster_path": "/p.jpg", "backdrop_path": "/b.jpg", "popularity": 47.2,
    }]
}
DETAIL = {
    "id": 1233413, "title": "Sinners", "release_date": "2025-04-16",
    "imdb_id": "tt31193180", "overview": "o", "runtime": 131,
    "vote_average": 7.4, "vote_count": 900,
    "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
    "genres": [{"id": 27, "name": "Horror"}, {"id": 28, "name": "Action"},
               {"id": 53, "name": "Thriller"}],
    "production_countries": [
        {"iso_3166_1": "US", "name": "United States of America"},
    ],
    "original_language": "en",
    "spoken_languages": [
        {"iso_639_1": "en", "name": "English"},
        {"iso_639_1": "yo", "name": "Yoruba"},
    ],
}

print("\n1. tmdb_find follows the search with a details call")


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class _Client:
    """Records calls; serves search vs movie/<id> appropriately."""

    calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        _Client.calls.append(url)
        if "/search/movie" in url:
            return _Resp(200, SEARCH)
        return _Resp(200, DETAIL)


P._cache = P._TTLCache(1)
_orig_client = P._client
P._client = lambda: _Client()
try:
    m = P.tmdb_find("Sinners", 2025, KEY)
finally:
    P._client = _orig_client

check("search endpoint was called", any("/search/movie" in c for c in _Client.calls))
check("details endpoint was called", any("/movie/1233413" in c for c in _Client.calls),
      str(_Client.calls))
check("movie metadata returned", m is not None)
if m is None:
    print("  (no metadata; remaining checks skipped)")
    sys.exit(1)

print("\n2. Genres come from the details call, not genre_ids")
check("genres populated", m.genres == ["Horror", "Action", "Thriller"], str(m.genres))
check("genres are not empty", bool(m.genres))

print("\n3. Countries populated and normalised to the short form")
check("countries populated", bool(m.countries), str(m.countries))
check("'United States of America' normalised to 'United States'",
      m.countries == ["United States"], str(m.countries))

print("\n4. Languages populated")
check("languages populated", bool(m.languages), str(m.languages))
check("original language mapped to a readable name", "English" in m.languages,
      str(m.languages))
check("spoken languages included", "Yoruba" in m.languages, str(m.languages))

print("\n5. Ratings and ids survive the merge")
check("imdb id from details", m.imdb_id == "tt31193180", str(m.imdb_id))
check("tmdb id", m.tmdb_id == 1233413, str(m.tmdb_id))
check("rating from vote_average", m.imdb_rating == 7.4, str(m.imdb_rating))
check("runtime", m.runtime == 131, str(m.runtime))

print("\n6. Details failure returns no result rather than a half-empty one")
# Superseded: this used to assert we still get a genre-less MovieMeta back. After
# v1.5.0, returning None is the correct outcome -- a film with no genres and no
# country is indistinguishable from "unresolved", and caching one made the
# backfill loop on the same rows forever.
P._cache = P._TTLCache(1)
calls: list[str] = []


class _FailClient(_Client):
    def get(self, url, params=None):
        calls.append(url)
        if "/search/movie" in url:
            return _Resp(200, SEARCH)
        return _Resp(500, {})


P._client = lambda: _FailClient()
try:
    m2 = P.tmdb_find("Sinners", 2025, KEY)
finally:
    P._client = _orig_client

check("returns None when details are unavailable", m2 is None, str(m2))
check("so the caller keeps the row blank and retries later", m2 is None)

print("\n6b. genre_ids are still used as a fallback when details carry no genres")
DETAIL_NOGENRE = {k: v for k, v in DETAIL.items() if k != "genres"}


class _CNoGenre(_Client):
    def get(self, url, params=None):
        _Client.calls.append(url)
        if "/search/movie" in url:
            return _Resp(200, SEARCH)
        return _Resp(200, DETAIL_NOGENRE)


P._cache = P._TTLCache(1)
P._client = lambda: _CNoGenre()
try:
    m3 = P.tmdb_find("Sinners", 2025, KEY)
finally:
    P._client = _orig_client

check("still resolves", m3 is not None)
check("genres recovered from genre_ids",
      m3 is not None and m3.genres == ["Horror", "Action", "Thriller"],
      str(m3.genres) if m3 else "None")

print("\n7. A genre rule matches a film enriched through this path")
from app.scoring import Scorer, parse_rules  # noqa: E402

rules = parse_rules([{"id": "g", "kind": "genre", "op": "in",
                      "value": ["Horror"], "points": 25}])
res = Scorer(rules).score({"title": "Sinners", "genres": m.genres,
                           "countries": m.countries, "languages": m.languages})
check("Horror rule fires", res.score == 25, str(res.score))

rules2 = parse_rules([{"id": "c", "kind": "country", "op": "in",
                       "value": ["United States"], "points": 15}])
res2 = Scorer(rules2).score({"title": "Sinners", "genres": m.genres,
                             "countries": m.countries, "languages": m.languages})
check("United States rule fires", res2.score == 15, str(res2.score))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)