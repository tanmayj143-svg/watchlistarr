"""Keyless movie metadata providers.

Priority: no login, no paid API.

1. **Cinemeta** (``v3-cinemeta.strem.io``) — the public Stremio metadata
   service. Returns genre, country, language, year, runtime, rating, IMDb id and
   TMDB id for an IMDb/TMDB id or by title search. Verified live in v1.0.0 dev:
   it is the richest keyless source available and needs no token.
2. **IMDb suggestion API** (``v3.sg.media-imdb.com``) — id, title, year, poster
   resolution by title. Used to turn a Letterboxd slug into an IMDb id.

An optional TMDB API key can be supplied in Settings; when present it is used
first, because TMDB is more precise on country-of-origin and language.
"""
from __future__ import annotations

import logging
import re
import time
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("watchlistarr.metadata")

# Dominant-language heuristic by country, used only when Cinemeta AND Wikidata
# both fail to report a language. Deliberately conservative: it maps only
# countries where one language overwhelmingly dominates production, and it is
# flagged on the candidate so the UI can tell a guess from a sourced value.
COUNTRY_LANGUAGE = {
    "China": "Mandarin",
    "Hong Kong": "Cantonese",
    "Taiwan": "Mandarin",
    "Japan": "Japanese",
    "South Korea": "Korean",
    "India": "Hindi",
    "France": "French",
    "Belgium": "French",
    "Germany": "German",
    "Austria": "German",
    "Switzerland": "German",
    "Italy": "Italian",
    "Spain": "Spanish",
    "Mexico": "Spanish",
    "Argentina": "Spanish",
    "Russia": "Russian",
    "Sweden": "Swedish",
    "Denmark": "Danish",
    "Norway": "Norwegian",
    "Finland": "Finnish",
    "Netherlands": "Dutch",
    "Poland": "Polish",
    "Czechia": "Czech",
    "Greece": "Greek",
    "Turkey": "Turkish",
    "Thailand": "Thai",
    "Brazil": "Portuguese",
    "Portugal": "Portuguese",
    "Iran": "Persian",
    "Egypt": "Arabic",
    "Ireland": "English",
    "United Kingdom": "English",
    "United States": "English",
    "Canada": "English",
    "Australia": "English",
}


CINEMETA_BASE = "https://v3-cinemeta.strem.io"
IMDB_SUGGEST = "https://v3.sg.media-imdb.com/suggestion/x/{query}.json"
TMDB_BASE = "https://api.themoviedb.org/3"


@dataclass
class MovieMeta:
    """Normalised metadata. Everything optional except title."""

    title: str
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    poster: str | None = None
    backdrop: str | None = None
    overview: str | None = None
    genres: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    director: str | None = None
    runtime: int | None = None
    imdb_rating: float | None = None
    votes: int | None = None
    popularity: float | None = None
    released: str | None = None
    provider: str = "none"
    slug: str | None = None
    meta_language_guessed: bool = False

    @property
    def release_year(self) -> int | None:
        if self.year:
            return self.year
        if self.released:
            m = re.match(r"(\d{4})", self.released)
            if m:
                return int(m.group(1))
        return None

    def to_json(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["year"] = self.release_year
        return d


class _TTLCache:
    """Tiny in-process TTL cache; metadata rarely changes and lookups are hot."""

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._d: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            hit = self._d.get(key)
            if not hit:
                return None
            ts, val = hit
            if time.time() - ts > self.ttl:
                self._d.pop(key, None)
                return None
            return val

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._d) > 20000:  # crude bound
                self._d.clear()
            self._d[key] = (time.time(), value)


_cache = _TTLCache(settings.metadata_cache_hours * 3600)


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "en-US,en;q=0.9"},
    )


def _split_csv(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Cinemeta
# --------------------------------------------------------------------------
def cinemeta_by_imdb(imdb_id: str) -> MovieMeta | None:
    key = f"cm:{imdb_id}"
    if (hit := _cache.get(key)) is not None:
        return hit
    try:
        with _client() as c:
            r = c.get(f"{CINEMETA_BASE}/meta/movie/{imdb_id}.json")
        if r.status_code != 200:
            return None
        meta = (r.json() or {}).get("meta") or {}
    except (httpx.HTTPError, ValueError):
        return None
    if not meta.get("name"):
        return None

    year = _as_int(meta.get("year"))
    out = MovieMeta(
        title=str(meta.get("name")),
        year=year,
        imdb_id=meta.get("imdb_id") or (imdb_id if imdb_id.startswith("tt") else None),
        tmdb_id=_as_int(meta.get("moviedb_id")),
        poster=_upgrade_poster(meta.get("poster")),
        backdrop=meta.get("background"),
        overview=meta.get("description"),
        genres=_split_csv(meta.get("genre")),
        countries=_split_csv(meta.get("country")),
        languages=_normalise_languages(meta.get("language")),
        director=", ".join(_split_csv(meta.get("director"))[:2]) or None,
        runtime=_parse_runtime(meta.get("runtime")),
        imdb_rating=_as_float(meta.get("imdbRating")),
        votes=_as_int(meta.get("imdbVotes")),
        popularity=_as_float(meta.get("popularity")),
        released=meta.get("released"),
        provider="cinemeta",
    )
    _cache.set(key, out)
    return out


def _parse_runtime(value: Any) -> int | None:
    if not value:
        return None
    m = re.search(r"(\d+)", str(value))
    return int(m.group(1)) if m else None


def _upgrade_poster(poster: str | None) -> str | None:
    """Cinemeta returns small posters; ask for the large variant."""
    if not poster:
        return None
    return poster.replace("/small/", "/medium/").replace("/thumb/", "/medium/")


def _normalise_languages(value: Any) -> list[str]:
    langs = _split_csv(value)
    out: list[str] = []
    for lang in langs:
        code, _, name = lang.partition(":")
        out.append((name or code).strip())
    return [l for l in out if l]


def cinemeta_search(title: str, year: int | None = None) -> MovieMeta | None:
    """Title search through Cinemeta; prefers an exact/year match."""
    from urllib.parse import quote

    key = f"cms:{title.lower()}:{year}"
    if (hit := _cache.get(key)) is not None:
        return hit
    try:
        with _client() as c:
            r = c.get(f"{CINEMETA_BASE}/catalog/movie/top/search={quote(title)}.json")
        if r.status_code != 200:
            return None
        metas = (r.json() or {}).get("metas") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not metas:
        return None

    def score(m: dict[str, Any]) -> int:
        s = 0
        name = str(m.get("name") or m.get("releaseInfo") and m.get("name") or "")
        if name.lower() == title.lower():
            s += 50
        if year and str(m.get("releaseInfo") or "") == str(year):
            s += 30
        if m.get("id", "").startswith("tt"):
            s += 5
        return s

    best = max(metas, key=score)
    best_id = best.get("id") or best.get("imdb_id")
    if best_id and str(best_id).startswith("tt"):
        full = cinemeta_by_imdb(str(best_id))
        if full:
            return full
    release = str(best.get("releaseInfo") or "")
    return MovieMeta(
        title=str(best.get("name") or title),
        year=_as_int(release[:4]) if release[:4].isdigit() else year,
        imdb_id=str(best_id) if best_id else None,
        poster=_upgrade_poster(best.get("poster")),
        provider="cinemeta",
    )


# --------------------------------------------------------------------------
# IMDb suggestion API
# --------------------------------------------------------------------------
def imdb_suggest(title: str, year: int | None = None) -> MovieMeta | None:
    from urllib.parse import quote

    key = f"imdbs:{title.lower()}:{year}"
    if (hit := _cache.get(key)) is not None:
        return hit
    try:
        with _client() as c:
            r = c.get(IMDB_SUGGEST.format(query=quote(title)))
        if r.status_code != 200:
            return None
        entries = (r.json() or {}).get("d") or []
    except (httpx.HTTPError, ValueError):
        return None

    def score(e: dict[str, Any]) -> int:
        s = 0
        name = str(e.get("l") or "")
        if name.lower() == title.lower():
            s += 50
        if year and str(e.get("y") or "") == str(year):
            s += 30
        if e.get("qid") in ("movie", "feature", "tvshort", "tvmovie"):
            s += 10
        return s

    if not entries:
        return None
    best = max(entries, key=score)
    if score(best) < 30:
        return None
    img = best.get("i") or {}
    out = MovieMeta(
        title=str(best.get("l") or title),
        year=_as_int(best.get("y")),
        imdb_id=best.get("id"),
        poster=img.get("imageUrl"),
        popularity=_as_float(best.get("rank")),
        released=best.get("q"),
        provider="imdb",
    )
    _cache.set(key, out)
    return out


# --------------------------------------------------------------------------
# Optional TMDB (only used when the user supplies a free key)
# --------------------------------------------------------------------------
# TMDB /search returns numeric genre_ids; /movie/<id> is the only place names
# appear. These ids are stable and public, so we can map them without another call.
_TMDB_GENRE_IDS = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western", 10759: "Action & Adventure",
    10765: "Sci-Fi & Fantasy", 10768: "War & Politics", 10763: "Documentary",
}

_TMDB_LANGUAGES = {
    "en": "English", "zh": "Mandarin", "cn": "Cantonese", "ja": "Japanese",
    "ko": "Korean", "fr": "French", "de": "German", "es": "Spanish",
    "it": "Italian", "ru": "Russian", "hi": "Hindi", "pt": "Portuguese",
    "da": "Danish", "sv": "Swedish", "pl": "Polish", "nl": "Dutch",
    "tr": "Turkish", "fa": "Persian", "ar": "Arabic", "la": "Latin",
}


def _genre_name(gid: Any) -> str | None:
    return _TMDB_GENRE_IDS.get(_as_int(gid))


def _language_name(code: str | None) -> str | None:
    if not code:
        return None
    return _TMDB_LANGUAGES.get(code, code)


_COUNTRY_NORMALISE = {
    "united states of america": "United States",
    "usa": "United States",
    "us": "United States",
    "united kingdom of great britain and northern ireland": "United Kingdom",
    "uk": "United Kingdom",
    "russian federation": "Russia",
    "republic of korea": "South Korea",
    "korea, republic of": "South Korea",
    "korea": "South Korea",
    "people's republic of china": "China",
    "prc": "China",
    "soviet union": "Russia",
    "czechoslovakia": "Czech Republic",
    "west germany": "Germany",
    "east germany": "Germany",
    "french republic": "France",
    "republic of india": "India",
    "brazil": "Brazil",
    "canada": "Canada",
    "italy": "Italy",
    "spain": "Spain",
    "mexico": "Mexico",
    "australia": "Australia",
    "sweden": "Sweden",
    "denmark": "Denmark",
    "norway": "Norway",
    "finland": "Finland",
    "poland": "Poland",
    "netherlands": "Netherlands",
    "taiwan": "Taiwan",
    "hong kong": "Hong Kong",
    "turkey": "Turkey",
    "iran": "Iran",
    "thailand": "Thailand",
    "ireland": "Ireland",
    "austria": "Austria",
    "belgium": "Belgium",
    "switzerland": "Switzerland",
    "greece": "Greece",
    "portugal": "Portugal",
    "argentina": "Argentina",
    "chile": "Chile",
    "hong kong sar china": "Hong Kong",
    "china": "China",
    "japan": "Japan",
    "india": "India",
    "germany": "Germany",
    "france": "France",
}


_COUNTRY_DEFAULT_LANGUAGE = {
    "United States": "English", "United Kingdom": "English", "India": "Hindi",
    "China": "Mandarin", "Hong Kong": "Cantonese", "Taiwan": "Mandarin",
    "Japan": "Japanese", "South Korea": "Korean", "France": "French",
    "Germany": "German", "Italy": "Italian", "Spain": "Spanish",
    "Russia": "Russian", "Brazil": "Portuguese", "Mexico": "Spanish",
    "Sweden": "Swedish", "Denmark": "Danish", "Norway": "Norwegian",
    "Finland": "Finnish", "Poland": "Polish", "Netherlands": "Dutch",
    "Turkey": "Turkish", "Iran": "Persian", "Egypt": "Arabic",
    "Nigeria": "English", "Canada": "English", "Australia": "English",
    "Belgium": "Dutch", "Austria": "German", "Switzerland": "German",
    "Greece": "Greek", "Portugal": "Portuguese", "Ireland": "English",
    "Thailand": "Thai", "Indonesia": "Indonesian", "Argentina": "Spanish",
    "Chile": "Spanish", "Colombia": "Spanish", "Romania": "Romanian",
    "Czech Republic": "Czech", "Hungary": "Hungarian", "Ukraine": "Ukrainian",
}


def _country_name(raw: str) -> str:
    return _COUNTRY_NORMALISE.get((raw or "").strip().lower(), (raw or "").strip())


def _tmdb_detail(tmdb_id: Any, api_key: str) -> MovieMeta | None:
    """Full TMDB record -> MovieMeta, using the details endpoint only.

    /search/movie returns numeric genre_ids and no country/language names, so the
    details endpoint is the authoritative source for genres, countries and
    languages. This is the single place that turns a TMDB payload into MovieMeta.
    """
    try:
        tid = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    if not tid:
        return None

    try:
        with _client() as c:
            d = c.get(f"{TMDB_BASE}/movie/{tid}", params={"api_key": api_key})
        if d.status_code != 200:
            return None
        payload = d.json() or {}
    except (httpx.HTTPError, ValueError):
        return None

    genres = [g["name"] for g in payload.get("genres") or [] if isinstance(g, dict) and g.get("name")]
    countries = [
        _country_name(p["name"])
        for p in payload.get("production_countries") or []
        if isinstance(p, dict) and p.get("name")
    ]
    countries = list(dict.fromkeys(countries))

    languages: list[str] = []
    if payload.get("original_language"):
        lang = _language_name(payload["original_language"])
        if lang:
            languages.append(lang)
    for entry in payload.get("spoken_languages") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("english_name") or entry.get("name")
        if name and name not in languages:
            languages.append(name)

    poster_path = payload.get("poster_path")
    backdrop_path = payload.get("backdrop_path")

    return MovieMeta(
        title=str(payload.get("title") or ""),
        year=_as_int((payload.get("release_date") or "")[:4]),
        tmdb_id=tid,
        imdb_id=payload.get("imdb_id") or None,
        poster=f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
        backdrop=f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else None,
        overview=payload.get("overview"),
        genres=genres,
        countries=countries,
        languages=languages,
        runtime=_as_int(payload.get("runtime")),
        imdb_rating=_as_float(payload.get("vote_average")),
        votes=_as_int(payload.get("vote_count")),
        popularity=_as_float(payload.get("popularity")),
        released=payload.get("release_date"),
        provider="tmdb",
    )


def _language_from_countries(countries: list[str]) -> str | None:
    """Heuristic dominant language from country of origin. Last resort only."""
    for c in countries or []:
        lang = _COUNTRY_DEFAULT_LANGUAGE.get(c)
        if lang:
            return lang
    return None


def tmdb_find(title: str, year: int | None, api_key: str) -> MovieMeta | None:
    """Find a film by title/year in TMDB and return full details."""
    if not api_key:
        return None
    key = f"tmdb:{title.lower()}:{year}:{api_key[-4:]}"
    if (hit := _cache.get(key)) is not None:
        return hit

    params: dict[str, Any] = {"api_key": api_key, "query": title, "include_adult": "false"}
    if year:
        params["year"] = year
    try:
        with _client() as c:
            r = c.get(f"{TMDB_BASE}/search/movie", params=params)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        return None

    # TMDB search is fuzzy and returns unrelated titles for obscure names -- a
    # search for "Kaul" surfaced The Bone Collector. Storing the wrong film poisons
    # every rule downstream, so pick the closest title and refuse anything that
    # does not plausibly match.
    wanted = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()

    def _rank_hit(h: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        name = re.sub(r"[^a-z0-9]+", " ", (h.get("title") or "").lower()).strip()
        if not name:
            return (0.0, h)
        if name == wanted:
            return (100.0, h)
        if name.startswith(wanted) or wanted.startswith(name):
            return (80.0, h)
        if wanted in name or name in wanted:
            return (60.0, h)
        a, b = set(wanted.split()), set(name.split())
        if a and b and len(a & b) / len(a | b) >= 0.5:
            return (40.0, h)
        return (0.0, h)

    ranked = sorted((_rank_hit(h) for h in results), key=lambda t: -t[0])
    if not ranked or ranked[0][0] <= 0:
        return None  # nothing resembles the title; leave the row blank and retry
    best = ranked[0][1]

    # Release year is part of the match key, not a hint. Remakes and same-title
    # films are common (a 1962 and a 1985 "Highway"), so when the caller knows the
    # year, only an exact match counts. The search query already biased by year;
    # this rejects anything that slipped through.
    if year:
        hit_year = _as_int((best.get("release_date") or "")[:4])
        if hit_year is not None and hit_year != int(year):
            exact = [h for h in results
                     if _as_int((h.get("release_date") or "")[:4]) == int(year)]
            if not exact:
                log.info("tmdb: no %r from %s (top hit was %s from %s)",
                         title, year, best.get("title"), hit_year)
                return None
            best = sorted((_rank_hit(h) for h in exact),
                          key=lambda t: -t[0])[0][1]

    out = _tmdb_detail(_as_int(best.get("id")), api_key)
    if out is None:
        return None

    # Fill genuine gaps from the search payload itself.
    if not out.genres:
        out.genres = [g for g in (_genre_name(i) for i in best.get("genre_ids") or []) if g]
    if out.popularity is None:
        out.popularity = _as_float(best.get("popularity"))
    if not out.overview:
        out.overview = best.get("overview")
    if out.year is None:
        out.year = _as_int((best.get("release_date") or "")[:4])
    if not out.released:
        out.released = best.get("release_date")
    if not out.title:
        out.title = str(best.get("title") or title)
    out.imdb_id = out.imdb_id or (best.get("imdb_id") or None)

    # Only cache a usable answer. Caching a genre-less result made every retry
    # return the same empty value, so the backfill looped forever on those rows.
    if out.genres or out.countries or out.imdb_id:
        _cache.set(key, out)
    return out


# --------------------------------------------------------------------------
# Public facade
# --------------------------------------------------------------------------
def resolve(
    *,
    title: str,
    year: int | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    slug: str | None = None,
    tmdb_key: str | None = None,
) -> MovieMeta | None:
    """Resolve one film from a single authoritative source per field.

    Standardisation rule: each field is owned by exactly one provider and is
    never assembled by appending values from several. Mixing sources made the
    same genre/country list differ per film and appended duplicates, which made
    rule results impossible to reason about.

    Precedence, highest first:
      1. TMDB  -- authoritative for genres, countries, languages, rating, dates.
      2. Cinemeta -- keyless fallback when no TMDB key is configured.
      3. IMDb suggestion -- last-ditch identification only.
      4. Wikidata -- language only, and only when nothing above supplied one.
      5. A country-derived language guess -- last resort, flagged as guessed.

    `slug` is the Letterboxd identifier, kept for provenance.
    """
    best: MovieMeta | None = None
    source_used: str | None = None

    # TMDB first when a key exists: it is the only provider that returns genre,
    # country and language names rather than just ids.
    if tmdb_key:
        best = tmdb_find(title, year, tmdb_key)
        if best is not None:
            source_used = "tmdb"
        elif imdb_id and str(imdb_id).startswith("tt"):
            by_imdb = tmdb_by_imdb(imdb_id, tmdb_key)
            if by_imdb is not None:
                best = by_imdb
                source_used = "tmdb"

    if best is None and imdb_id and str(imdb_id).startswith("tt"):
        # Identifiers are still worth a lookup: matching an IMDb id to its TMDB
        # record stays within the single TMDB source.
        best = tmdb_by_imdb(imdb_id, tmdb_key)
        source_used = "tmdb" if best is not None else None

    if best is None:
        # No other provider is consulted. Storing Cinemeta/IMDb/Wikidata values
        # alongside TMDB produced inconsistent genre and country lists for the
        # same provider across films, so a field is now either present from TMDB
        # or empty and retryable.
        return None

    # Identifiers are authoritative from the source we matched, with the
    # caller's own ids filling genuine gaps. Never overwrite a found id.
    if best.imdb_id is None and imdb_id and str(imdb_id).startswith("tt"):
        best.imdb_id = str(imdb_id)
    if best.tmdb_id is None and tmdb_id:
        best.tmdb_id = tmdb_id
    if best.year is None and year:
        best.year = year
    if best.slug is None and slug:
        best.slug = slug

    # No inference: a field is present from TMDB or empty. The UI shows
    # "not tagged" rather than a guessed value.
    best.provider = source_used or best.provider
    return best


def tmdb_by_imdb(imdb_id: str, api_key: str) -> MovieMeta | None:
    """Look a film up by IMDb id and return full TMDB details."""
    if not api_key or not imdb_id:
        return None
    key = f"tmdb-imdb:{imdb_id}:{api_key[-4:]}"
    if (hit := _cache.get(key)) is not None:
        return hit

    try:
        with _client() as c:
            r = c.get(f"{TMDB_BASE}/find", params={
                "api_key": api_key, "external_source": "imdb_id", "external_id": imdb_id,
            })
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("movie_results") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        return None

    detail = _tmdb_detail(_as_int(results[0].get("id")), api_key)
    if detail is None:
        return None
    _cache.set(key, detail)
    return detail



def _merge(primary: MovieMeta | None, extra: MovieMeta) -> MovieMeta:
    """Fill gaps in `primary` from `extra` without overwriting real data."""
    if primary is None:
        return extra
    for f in (
        "year", "imdb_id", "tmdb_id", "poster", "backdrop", "overview",
        "director", "runtime", "imdb_rating", "votes", "popularity", "released", "slug",
    ):
        if getattr(primary, f) in (None, ""):
            setattr(primary, f, getattr(extra, f))
    for f in ("genres", "countries", "languages"):
        if not getattr(primary, f) and getattr(extra, f):
            setattr(primary, f, list(getattr(extra, f)))
    return primary