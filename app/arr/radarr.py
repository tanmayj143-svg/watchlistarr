"""Radarr client — used for capacity-aware rules.

The brief asks for rules keyed on "GB of disk space left" and "movies already in
the library", so Watchlistarr reads Radarr's own view of reality rather than
guessing:

    GET /api/v3/system/status        -> version
    GET /api/v3/diskspace            -> [{path, freeSpace, totalSpace}]
    GET /api/v3/movie               -> every movie (count + size)
    GET /api/v3/rootfolder          -> [{path, freeSpace, accessible}]

Auth is `X-Api-Key`. Radarr is optional: without it, capacity rules are
evaluated as "unknown" and simply do not gate requests (documented in
docs/SCORING.md).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("watchlistarr.radarr")


class RadarrError(RuntimeError):
    pass


@dataclass
class RadarrCapacity:
    """Snapshot used by capacity-aware scoring rules and library matching."""

    available: bool = False
    free_bytes: int = 0
    total_bytes: int = 0
    movie_count: int = 0
    movie_bytes: int = 0
    # movie id -> size, so "size of the N newest" rules can work
    movies: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    # --- library identity indexes -------------------------------------
    # Populated by `capacity()`; used to recognise films Radarr already knows
    # about, including ones whose file was deleted (they remain in the DB with
    # hasFile=false until removed from Radarr itself).
    tmdb_ids: set[int] = field(default_factory=set)
    imdb_ids: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)          # normalised lowercase
    years: dict[str, int] = field(default_factory=dict)     # normalised title -> year
    with_file: set[str] = field(default_factory=set)       # normalised titles present on disk
    # tmdb id -> True when Radarr actually has a file. A tracked movie without a
    # file is monitored-but-missing: still queued, so the operator does not have
    # it. Keeping the two apart is what stops the Radarr guard from swallowing
    # the whole watchlist.
    tmdb_with_file: set[int] = field(default_factory=set)
    tracked_missing: set[int] = field(default_factory=set)
    # imdb id -> tmdb id, so an IMDb-keyed lookup can still ask whether the film
    # has a file. (imdb_ids itself stays a set of ids.)
    imdb_to_tmdb: dict[str, int] = field(default_factory=dict)

    def knows(self, *, tmdb_id: Any = None, imdb_id: str | None = None,
              title: str | None = None, year: Any = None) -> tuple[bool, str | None]:
        """Does Radarr already have this film *on disk*? Returns (known, reason).

        Being tracked by Radarr is not the same as existing. On the live instance
        Radarr held 765 movies but only 276 had files -- the rest were
        monitored-but-missing, i.e. queued for download. Treating those as
        "already have it" made 529 of 807 candidates ineligible, so Request Next
        had nothing to request.

        Only a film with an actual file counts as existing. A tracked-but-missing
        film is deliberately still a candidate, since the operator does not have
        it.
        """
        state = self.movie_state(tmdb_id=tmdb_id, imdb_id=imdb_id,
                                 title=title, year=year)
        if state == "file":
            return True, "already in Radarr with a file"
        return False, None

    def tracked_but_missing(self, *, tmdb_id: Any = None, imdb_id: str | None = None,
                            title: str | None = None, year: Any = None) -> str | None:
        """Reason string when Radarr tracks the film but has no file for it."""
        if self.movie_state(tmdb_id=tmdb_id, imdb_id=imdb_id,
                            title=title, year=year) == "missing":
            return "in Radarr but not downloaded yet"
        return None

    def movie_state(self, *, tmdb_id: Any = None, imdb_id: str | None = None,
                    title: str | None = None, year: Any = None) -> str | None:
        """'file' | 'missing' | None for a film, matched on ids then title+year."""
        if tmdb_id and str(tmdb_id).strip().isdigit() and int(tmdb_id) in self.tmdb_ids:
            return "file" if int(tmdb_id) in self.tmdb_with_file else "missing"
        if imdb_id and str(imdb_id).strip() in self.imdb_ids:
            # imdb_ids is a set of ids; the id->tmdb map carries the file state.
            tid = self.imdb_to_tmdb.get(str(imdb_id).strip())
            if tid is not None and tid in self.tmdb_with_file:
                return "file"
            norm2 = _norm_title(title)
            if norm2 and norm2 in self.with_file:
                return "file"
            return "missing"

        norm = _norm_title(title)
        if norm and norm in self.titles:
            # Title matching needs the year to agree: remakes share titles.
            known_year = self.years.get(norm)
            try:
                want_year = int(year) if year else None
            except (TypeError, ValueError):
                want_year = None
            if want_year is not None and known_year is not None and want_year != known_year:
                return None
            return "file" if norm in self.with_file else "missing"
        return None

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024**3)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024**3)

    @property
    def used_gb(self) -> float:
        return max(self.total_gb - self.free_gb, 0.0)

    @property
    def used_fraction(self) -> float:
        return (self.used_gb / self.total_gb) if self.total_gb > 0 else 0.0


_TITLE_NOISE = re.compile(r"[^a-z0-9 ]+")


def _norm_title(title: str | None) -> str:
    """Lowercase, strip punctuation/articles so 'The Thing' == 'Thing, The'."""
    if not title:
        return ""
    t = _TITLE_NOISE.sub(" ", str(title).lower())
    t = re.sub(r"\b(the|a|an)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _norm_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith("http"):
        base = "http://" + base
    return base


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key, "Accept": "application/json"}


class RadarrClient:
    def __init__(self, url: str, api_key: str, timeout: int = 30):
        self.base = _norm_base(url)
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        if not self.base or not self.api_key:
            raise RadarrError("Radarr URL and API key are required.")

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, follow_redirects=True)

    def status(self) -> dict[str, Any]:
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/api/v3/system/status", headers=_headers(self.api_key))
        except httpx.HTTPError as exc:
            raise RadarrError(f"Cannot reach Radarr at {self.base}: {exc}") from exc
        if r.status_code in (401, 403):
            raise RadarrError("Radarr rejected the API key (HTTP %s)." % r.status_code)
        if r.status_code != 200:
            raise RadarrError(f"Radarr status returned HTTP {r.status_code}.")
        try:
            return r.json()
        except ValueError as exc:
            raise RadarrError("Radarr returned a non-JSON status response.") from exc

    def test(self) -> tuple[bool, str]:
        try:
            data = self.status()
        except RadarrError as exc:
            return False, str(exc)
        return True, f"Connected — Radarr v{data.get('version', '?')}"

    def capacity(self) -> RadarrCapacity:
        """Disk + library snapshot. Never raises — capacity rules must not
        break the request pipeline when Radarr is down."""
        cap = RadarrCapacity()
        try:
            with self._client() as c:
                d = c.get(f"{self.base}/api/v3/diskspace", headers=_headers(self.api_key))
                m = c.get(f"{self.base}/api/v3/movie", headers=_headers(self.api_key))
        except httpx.HTTPError as exc:
            cap.error = str(exc)
            return cap

        if d.status_code == 200:
            try:
                for disk in d.json() or []:
                    path = disk.get("path") or ""
                    free = int(disk.get("freeSpace") or 0)
                    total = int(disk.get("totalSpace") or 0)
                    # Ignore tiny utility mounts; sum the real library volumes.
                    if total > 0 and total > 5 * 1024**3:
                        cap.free_bytes += free
                        cap.total_bytes += total
                    log.debug(
                        "radarr disk %s free=%.1fGB total=%.1fGB",
                        path, free / 1024**3, total / 1024**3,
                    )
            except (ValueError, TypeError):
                log.warning("radarr diskspace payload was not parseable")

        if m.status_code == 200:
            try:
                movies = m.json() or []
                cap.movie_count = len(movies)
                for mv in movies:
                    size = int(mv.get("size") or 0)
                    cap.movie_bytes += size
                    has_file = bool(mv.get("hasFile"))
                    if mv.get("tmdbId"):
                        try:
                            tid = int(mv["tmdbId"])
                            cap.tmdb_ids.add(tid)
                            # Only a real file counts as "already have it".
                            if has_file:
                                cap.tmdb_with_file.add(tid)
                            else:
                                cap.tracked_missing.add(tid)
                        except (TypeError, ValueError):
                            pass
                    if mv.get("imdbId"):
                        iid = str(mv["imdbId"]).strip()
                        cap.imdb_ids.add(iid)
                        if mv.get("tmdbId"):
                            try:
                                cap.imdb_to_tmdb[iid] = int(mv["tmdbId"])
                            except (TypeError, ValueError):
                                pass
                    norm = _norm_title(mv.get("title"))
                    if norm:
                        cap.titles.add(norm)
                        if mv.get("year"):
                            try:
                                cap.years[norm] = int(mv["year"])
                            except (TypeError, ValueError):
                                pass
                        if has_file:
                            cap.with_file.add(norm)
                    cap.movies.append(
                        {
                            "id": mv.get("id"),
                            "title": mv.get("title"),
                            "size": size,
                            "hasFile": bool(mv.get("hasFile")),
                            "year": mv.get("year"),
                            "tmdbId": mv.get("tmdbId"),
                        }
                    )
            except (ValueError, TypeError):
                log.warning("radarr movie payload was not parseable")
        cap.available = cap.total_bytes > 0
        if not cap.available:
            cap.error = cap.error or "Radarr reported no usable diskspace"
        return cap