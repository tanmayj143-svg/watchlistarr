"""Movie metadata — TMDB only (operator decision, 2026-10-14).

Every field (title, year, ids, genres, countries, languages, rating, dates,
poster) comes from exactly one source: TMDB, via the free API key configured in
Settings. No Cinemeta, no IMDb suggestion, no Wikidata, no country-derived
language guesses -- mixing sources made the same field differ per film and made
rule results impossible to reason about.

A TMDB API key is REQUIRED for enrichment. Without one `resolve()` returns None,
rows are stored unresolved, and later syncs retry them (backfill_metadata).

Entry points:
    resolve(title, year, imdb_id, tmdb_id, slug, tmdb_key) -> MovieMeta | None
    tmdb_find(title, year, api_key)                         -> by title search
    tmdb_by_imdb(imdb_id, api_key)                          -> by IMDb id
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

# --------------------------------------------------------------------------
# TMDB -- the single metadata source
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

# Complete ISO-639-1 map (generated from TMDB /configuration/languages,
# 186 entries). Prevents raw codes like 'ta'/'mr'/'my' leaking into the
# database as if they were language names.
_TMDB_LANGUAGES = {
    "aa": "Afar", "ab": "Abkhazian", "ae": "Avestan",
    "af": "Afrikaans", "ak": "Akan", "am": "Amharic",
    "an": "Aragonese", "ar": "Arabic", "as": "Assamese",
    "av": "Avaric", "ay": "Aymara", "az": "Azerbaijani",
    "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian",
    "bi": "Bislama", "bm": "Bambara", "bn": "Bengali",
    "bo": "Tibetan", "br": "Breton", "bs": "Bosnian",
    "ca": "Catalan", "ce": "Chechen", "ch": "Chamorro",
    "cn": "Cantonese", "co": "Corsican", "cr": "Cree",
    "cs": "Czech", "cu": "Slavic", "cv": "Chuvash",
    "cy": "Welsh", "da": "Danish", "de": "German",
    "dv": "Divehi", "dz": "Dzongkha", "ee": "Ewe",
    "el": "Greek", "en": "English", "eo": "Esperanto",
    "es": "Spanish", "et": "Estonian", "eu": "Basque",
    "fa": "Persian", "ff": "Fulah", "fi": "Finnish",
    "fj": "Fijian", "fo": "Faroese", "fr": "French",
    "fy": "Frisian", "ga": "Irish", "gd": "Gaelic",
    "gl": "Galician", "gn": "Guarani", "gu": "Gujarati",
    "gv": "Manx", "ha": "Hausa", "he": "Hebrew",
    "hi": "Hindi", "ho": "Hiri Motu", "hr": "Croatian",
    "ht": "Haitian; Haitian Creole", "hu": "Hungarian", "hy": "Armenian",
    "hz": "Herero", "ia": "Interlingua", "id": "Indonesian",
    "ie": "Interlingue", "ig": "Igbo", "ii": "Yi",
    "ik": "Inupiaq", "io": "Ido", "is": "Icelandic",
    "it": "Italian", "iu": "Inuktitut", "ja": "Japanese",
    "jv": "Javanese", "ka": "Georgian", "kg": "Kongo",
    "ki": "Kikuyu", "kj": "Kuanyama", "kk": "Kazakh",
    "kl": "Kalaallisut", "km": "Khmer", "kn": "Kannada",
    "ko": "Korean", "kr": "Kanuri", "ks": "Kashmiri",
    "ku": "Kurdish", "kv": "Komi", "kw": "Cornish",
    "ky": "Kirghiz", "la": "Latin", "lb": "Letzeburgesch",
    "lg": "Ganda", "li": "Limburgish", "ln": "Lingala",
    "lo": "Lao", "lt": "Lithuanian", "lu": "Luba-Katanga",
    "lv": "Latvian", "mg": "Malagasy", "mh": "Marshall",
    "mi": "Maori", "mk": "Macedonian", "ml": "Malayalam",
    "mn": "Mongolian", "mo": "Moldavian", "mr": "Marathi",
    "ms": "Malay", "mt": "Maltese", "my": "Burmese",
    "na": "Nauru", "nb": "Norwegian Bokmål", "nd": "Ndebele",
    "ne": "Nepali", "ng": "Ndonga", "nl": "Dutch",
    "nn": "Norwegian Nynorsk", "no": "Norwegian", "nr": "Ndebele",
    "nv": "Navajo", "ny": "Chichewa; Nyanja", "oc": "Occitan",
    "oj": "Ojibwa", "om": "Oromo", "or": "Oriya",
    "os": "Ossetian; Ossetic", "pa": "Punjabi", "pi": "Pali",
    "pl": "Polish", "ps": "Pushto", "pt": "Portuguese",
    "qu": "Quechua", "rm": "Raeto-Romance", "rn": "Rundi",
    "ro": "Romanian", "ru": "Russian", "rw": "Kinyarwanda",
    "sa": "Sanskrit", "sc": "Sardinian", "sd": "Sindhi",
    "se": "Northern Sami", "sg": "Sango", "sh": "Serbo-Croatian",
    "si": "Sinhalese", "sk": "Slovak", "sl": "Slovenian",
    "sm": "Samoan", "sn": "Shona", "so": "Somali",
    "sq": "Albanian", "sr": "Serbian", "ss": "Swati",
    "st": "Sotho", "su": "Sundanese", "sv": "Swedish",
    "sw": "Swahili", "ta": "Tamil", "te": "Telugu",
    "tg": "Tajik", "th": "Thai", "ti": "Tigrinya",
    "tk": "Turkmen", "tl": "Tagalog", "tn": "Tswana",
    "to": "Tonga", "tr": "Turkish", "ts": "Tsonga",
    "tt": "Tatar", "tw": "Twi", "ty": "Tahitian",
    "ug": "Uighur", "uk": "Ukrainian", "ur": "Urdu",
    "uz": "Uzbek", "ve": "Venda", "vi": "Vietnamese",
    "vo": "Volapük", "wa": "Walloon", "wo": "Wolof",
    "xh": "Xhosa", "yi": "Yiddish", "yo": "Yoruba",
    "za": "Zhuang", "zh": "Mandarin", "zu": "Zulu",
}


def _genre_name(gid: Any) -> str | None:
    return _TMDB_GENRE_IDS.get(_as_int(gid))


def _language_name(code: str | None) -> str | None:
    if not code:
        return None
    return _TMDB_LANGUAGES.get(code, code)


_PLACEHOLDER_LANGUAGES = {"no language", "none", "unknown", ""}


def _merge_language_forms(languages: list[str]) -> list[str]:
    """Collapse ISO-code entries into their named form; drop placeholders.

    TMDB can report one film's language as 'ta' (original_language) AND 'Tamil'
    (spoken_languages). Storing both double-counted the language on the
    dashboard and showed raw codes as if they were languages. A code entry is
    stored only as its resolved name; unmappable short codes are dropped; and
    placeholders like 'No Language' are removed entirely. Order preserved.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        low = name.strip().lower()
        if not low or low in _PLACEHOLDER_LANGUAGES or low in seen:
            return
        seen.add(low)
        out.append(name.strip())

    for lang in languages or []:
        s = (lang or "").strip()
        if not s:
            continue
        if re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2,4})?", s):
            # ISO-code-shaped: store only its resolved name.
            name = _TMDB_LANGUAGES.get(s.lower())
            if name:
                _add(name)
            # An unmappable short code is dropped: a raw code is never a useful
            # rule value and only pollutes the dashboard.
            continue
        _add(s)
    return out


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
    # v1.7.1: TMDB sometimes carries only the ISO code in original_language
    # ("ta", "mr", "my") when the code is missing from our small name map, and
    # spoken_languages may carry the same language under its full English name
    # ("Tamil"). The old code stored BOTH, so the dashboard counted the same
    # language twice and showed raw codes as if they were languages. Merge
    # code-form and name-form entries, then drop placeholders.
    languages = _merge_language_forms(languages)

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
    """Resolve one film from TMDB. A TMDB key is REQUIRED.

    Precedence: direct tmdb_id lookup when available (cheapest and exact),
    then IMDb-id -> TMDB find, then title/year search. Title search is
    validated against the requested title and the year is part of the match
    key, so a 1962 "Highway" can never store a 2014 "Highway".

    Returns None when there is no key, the lookup fails, or nothing plausibly
    matches -- callers store the row as unresolved and a later sync retries.
    """
    if not tmdb_key:
        log.info(
            "tmdb: no API key configured; cannot enrich %r "
            "(add a free TMDB key in Settings)", title,
        )
        return None

    best: MovieMeta | None = None

    if tmdb_id:
        try:
            best = _tmdb_detail(int(tmdb_id), tmdb_key)
        except (TypeError, ValueError):
            best = None

    if best is None and imdb_id and str(imdb_id).startswith("tt"):
        best = tmdb_by_imdb(str(imdb_id), tmdb_key)

    if best is None:
        best = tmdb_find(title, year, tmdb_key)

    if best is None:
        return None

    # Never overwrite an id the matched source found; fill genuine gaps from
    # the caller's own identifiers.
    if best.imdb_id is None and imdb_id and str(imdb_id).startswith("tt"):
        best.imdb_id = str(imdb_id)
    if best.tmdb_id is None and tmdb_id:
        best.tmdb_id = str(tmdb_id)
    if best.year is None and year:
        best.year = year
    if best.slug is None and slug:
        best.slug = slug

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
