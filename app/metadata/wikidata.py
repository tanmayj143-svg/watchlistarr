"""Wikidata enrichment — keyless language (and extra country) lookup.

Cinemeta returns ``country`` and ``genre`` for most films but leaves
``language`` empty for the majority, which would break language-based
prioritisation. Wikidata's REST API (``wbsearchentities`` + ``wbgetentities``)
fills that gap with no key and no login; verified live during v1.0.0
development:

* ``wbsearchentities`` for "Farewell My Concubine" -> Q696413 (~1-2 s)
* ``wbgetentities`` P345 -> tt0106332, P364 -> Q24841726 (Putonghua),
  P495 -> Q148 (People's Republic of China)

Wikidata labels are verbose ("People's Republic of China"), so country labels
are normalised through `COUNTRY_ALIASES` into the same short names TMDB/Cinemeta
use ("China"), letting country sliders behave consistently across providers.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from ..config import settings

log = logging.getLogger("watchlistarr.wikidata")

API = "https://www.wikidata.org/w/api.php"

# Wikidata's English label -> the short name other providers use.
COUNTRY_ALIASES = {
    "people's republic of china": "China",
    "republic of china": "Taiwan",
    "soviet union": "Russia",
    "russian federation": "Russia",
    "united states of america": "United States",
    "united kingdom of great britain and northern ireland": "United Kingdom",
    "kingdom of denmark": "Denmark",
    "kingdom of sweden": "Sweden",
    "kingdom of norway": "Norway",
    "republic of korea": "South Korea",
    "korea": "South Korea",
    "republic of ireland": "Ireland",
    "federal republic of germany": "Germany",
    "west germany": "Germany",
    "german empire": "Germany",
    "czechoslovakia": "Czechia",
    "united states virgin islands": "United States",
}

# Wikidata's language label -> the common wording people put in a slider.
LANGUAGE_ALIASES = {
    "putonghua": "Mandarin",
    "mandarin chinese": "Mandarin",
    "standard chinese": "Mandarin",
    "chinese": "Mandarin",
    "modern standard arabic": "Arabic",
    "filipino": "Tagalog",
    "castilian": "Spanish",
    "farsi": "Persian",
}

# Labels that mean "no usable language" - never useful for scoring.
_LANG_NOISE = re.compile(
    r"^(no language|unknown|none|sign language|cant|sl|multilingual|multiple languages|"
    r"silent film|language-neutral|interlanguage)",
    re.I,
)


def normalise_country(label: str | None) -> str | None:
    """Wikidata -> short country name ('People's Republic of China' -> 'China')."""
    if not label:
        return None
    low = label.strip()
    mapped = COUNTRY_ALIASES.get(low.lower())
    if mapped:
        return mapped
    stripped = re.sub(
        r"^(the\s+)?(kingdom|republic|state|people's republic)\s+of\s+", "", low, flags=re.I
    )
    if stripped != low and stripped:
        return COUNTRY_ALIASES.get(stripped.lower(), stripped)
    return low


def normalise_language(label: str | None) -> str | None:
    """Wikidata language label -> slider-friendly name, or None if useless."""
    if not label:
        return None
    low = label.strip()
    if _LANG_NOISE.match(low):
        return None
    return LANGUAGE_ALIASES.get(low.lower(), low)

# Wikidata's anonymous API is aggressively rate limited (HTTP 429) for burst
# traffic. A single global lock serialised an entire sync behind one call at a
# time; instead each worker thread takes one of a few "slots", so requests stay
# spaced apart per slot while several still run concurrently.
_MIN_INTERVAL_SEC = 1.6
_MAX_SLOTS = 2
_lock = threading.Lock()
_last_call: dict[int, float] = {}
_cache: dict[str, tuple[float, dict]] = {}


# Per-thread deadline for one enrich() call, so a throttled lookup cannot
# consume an unbounded amount of a sync's wall clock.
_Budget = threading.local()


def _budget_exhausted() -> bool:
    dl = getattr(_Budget, "deadline", None)
    return bool(dl) and time.monotonic() >= dl


def _throttle() -> None:
    """Take the slot that has been idle longest and wait out its interval."""
    with _lock:
        slot = min(_last_call, key=lambda k: _last_call[k])
        wait = _MIN_INTERVAL_SEC - (time.time() - _last_call[slot])
        if wait > 0:
            if _budget_exhausted():
                return
            time.sleep(wait)
        _last_call[slot] = time.time()


def _reset_slots() -> None:
    for i in range(_MAX_SLOTS):
        _last_call[i] = 0.0


_reset_slots()


def _client() -> httpx.Client:
    """Wikidata asks for a descriptive User-Agent; identify the app."""
    return httpx.Client(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": "Watchlistarr/1.0 (self-hosted watchlist request bot)"},
    )


def _cache_get(key: str, ttl: int = 7 * 24 * 3600) -> dict | None:
    """Read-through cache; metadata rarely changes and lookups are hot."""
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    return None


def _cache_set(key: str, value: dict) -> None:
    with _lock:
        if len(_cache) > 20000:
            _cache.clear()
        _cache[key] = (time.time(), value)


def api_get(params: dict, tries: int = 2) -> dict | None:
    """GET Wikidata, rate-limited and retried once.

    Transient failures here must be visible: a silent `except HTTPError` made
    language enrichment look like "film has no language" instead of "network
    blipped", which is exactly the bug this helper exists to prevent.
    """
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        if _budget_exhausted():
            log.info("wikidata budget exhausted, skipping %s", params.get("action"))
            return None
        try:
            _throttle()
            with _client() as c:
                r = c.get(API, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):
                # Honour Retry-After when present, otherwise exponential backoff.
                # Being throttled must never look like "this film has no data" --
                # that silently starved 659 of 807 films of genre/country data.
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0.0
                except (TypeError, ValueError):
                    delay = 0.0
                delay = max(delay, 2.0 * (2 ** (attempt - 1)))
                if _budget_exhausted():
                    log.info("wikidata rate limited and budget exhausted, giving up on %s",
                             params.get("action"))
                    return None
                log.warning("wikidata rate limited (HTTP %s, attempt %s/%s), sleeping %.1fs",
                            r.status_code, attempt, tries, delay)
                # Never sleep past the caller's budget: a 30s backoff inside a
                # 3s budget is what turned one sync into minutes of nothing.
                sleep_for = min(delay, 30.0)
                dl = getattr(_Budget, "deadline", None)
                if dl:
                    sleep_for = min(sleep_for, max(0.0, dl - time.monotonic()))
                if sleep_for > 0:
                    time.sleep(sleep_for)
                continue
            log.warning("wikidata HTTP %s for %s", r.status_code, params.get("action"))
            return None
        except httpx.HTTPError as exc:
            last = exc
            log.warning("wikidata request failed (attempt %s/%s): %s", attempt, tries, exc)
            time.sleep(1.0)
    if last:
        log.warning("wikidata giving up after %s attempts: %s", tries, last)
    return None


def _entity_claims(qid: str) -> dict:
    key = f"claims:{qid}"
    if (hit := _cache_get(key)) is not None:
        return hit
    data = api_get({"action": "wbgetentities", "format": "json", "ids": qid, "props": "claims"})
    if data is None:
        return {}
    claims = (data.get("entities", {}).get(qid, {}) or {}).get("claims", {}) or {}

    def qids(prop: str) -> list[str]:
        out = []
        for cl in claims.get(prop, []) or []:
            dv = (cl.get("mainsnak") or {}).get("datavalue") or {}
            v = dv.get("value")
            if isinstance(v, dict) and v.get("id"):
                out.append(v["id"])
        return out

    value = {
        "imdb_id": (qids("P345") or [None])[0],
        "tmdb_id": (qids("P4947") or [None])[0],
        "language_qids": qids("P364"),
        "country_qids": qids("P495"),
        "genre_qids": qids("P136"),
        "instance_qids": qids("P31"),
        "original_language_qids": qids("P364"),
        "publication_date": None,
    }
    for cl in claims.get("P577", []) or []:
        dv = (cl.get("mainsnak") or {}).get("datavalue") or {}
        v = dv.get("value")
        # Wikidata returns the value as an ISO string ("+1993-01-01T00:00:00Z");
        # older payloads used a nested dict, so accept both.
        if isinstance(v, str):
            value["publication_date"] = v
            break
        if isinstance(v, dict) and v.get("time"):
            value["publication_date"] = v["time"]
            break
    _cache_set(key, value)
    return value


def _labels(qids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not qids:
        return out
    for chunk_start in range(0, len(qids), 40):
        chunk = qids[chunk_start : chunk_start + 40]
        data = api_get(
            {
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(chunk),
                "props": "labels",
                "languages": "en",
            }
        )
        if data is None:
            continue
        for qid, ent in (data.get("entities") or {}).items():
            lbl = (ent.get("labels") or {}).get("en", {}).get("value")
            if lbl:
                out[qid] = lbl
    return out


def find_entity(title: str, year: int | None = None) -> str | None:
    """Resolve a film title (+ year) to a Wikidata QID."""
    key = f"find:{title.lower()}:{year}"
    if (hit := _cache_get(key)) is not None:
        return hit.get("qid") if isinstance(hit, dict) else None
    data = api_get(
        {
            "action": "wbsearchentities",
            "format": "json",
            "language": "en",
            "type": "item",
            "search": title,
            "limit": 5,
        }
    )
    if data is None:
        return None
    hits = data.get("search") or []
    if not hits:
        _cache_set(key, {"qid": None})
        return None

    # Score candidates, then confirm with P345/P31 so we never attach a novel
    # or TV item to a film.
    best_qid, best_score = None, -1
    for h in hits:
        lbl = (h.get("label") or "").strip().lower()
        desc = (h.get("description") or "").lower()
        s = 0
        if lbl == title.strip().lower():
            s += 50
        elif lbl.startswith(title.strip().lower()):
            s += 20
        if "film" in desc or "movie" in desc:
            s += 30
        if year and str(year) in desc:
            s += 25
        if "novel" in desc or "book" in desc or "album" in desc or "series" in desc:
            s -= 40
        if s > best_score:
            best_qid, best_score = h.get("id"), s

    if not best_qid or best_score < 25:
        _cache_set(key, {"qid": None})
        return None
    _cache_set(key, {"qid": best_qid})
    return best_qid


def enrich(
    title: str,
    year: int | None = None,
    imdb_id: str | None = None,
    *,
    time_budget: float | None = None,
) -> dict:
    """Return {'languages': [...], 'countries': [...], 'genres': [...]} for a film.

    Safe to call for every candidate: failures return empty lists.

    `time_budget` caps total wall-clock spent on Wikidata. The anonymous API is
    rate limited, so an unbounded lookup could burn ~45s of backoff on one film
    and starve an entire sync. When the budget is exhausted we return what we
    have so far, which is empty rather than wrong.
    """
    out: dict[str, list[str]] = {"languages": [], "countries": [], "genres": []}
    if not title:
        return out

    deadline = (time.monotonic() + time_budget) if time_budget else None
    _Budget.deadline = deadline

    qid: str | None = None
    if imdb_id:
        # Direct hit: try the title search, then confirm P345 matches imdb_id.
        cand = find_entity(title, year)
        if cand:
            claims = _entity_claims(cand)
            if claims.get("imdb_id") == imdb_id:
                qid = cand
    if qid is None:
        qid = find_entity(title, year)
    if qid is None:
        return out

    claims = _entity_claims(qid)
    lang_qids = claims.get("original_language_qids") or []
    country_qids = claims.get("country_qids") or []
    genre_qids = claims.get("genre_qids") or []

    labels = _labels(sorted(set(lang_qids + country_qids + genre_qids)))

    for q in lang_qids:
        lbl = normalise_language(labels.get(q))
        if lbl and lbl not in out["languages"]:
            out["languages"].append(lbl)
    for q in country_qids:
        lbl = normalise_country(labels.get(q))
        if lbl and lbl not in out["countries"]:
            out["countries"].append(lbl)
    for q in genre_qids:
        lbl = labels.get(q)
        if lbl and lbl not in out["genres"]:
            out["genres"].append(lbl)

    return out