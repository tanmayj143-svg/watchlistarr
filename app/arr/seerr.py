"""Seerr (Jellyseerr / Overseerr) client.

Speaks the common v1 API both forks share:

    GET  {url}/api/v1/status                       -> version + key validity
    POST {url}/api/v1/request                      -> create a request
    GET  {url}/api/v1/request/{id}                 -> request state
    GET  {url}/api/v1/request?take=...             -> list requests
    POST {url}/api/v1/settings/request             -> read per-root settings

Auth is `X-Api-Key`. Requests are created with `mediaType=movie` and a
`mediaId` that is the TMDB id (what both forks expect).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("watchlistarr.seerr")


class SeerrError(RuntimeError):
    """API call to Seerr failed."""


@dataclass
class SeerrRequestResult:
    ok: bool
    request_id: int | None = None
    status: str | None = None
    detail: str | None = None
    already: bool = False


def _norm_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith("http"):
        base = "http://" + base
    return base


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key, "Accept": "application/json"}


class SeerrClient:
    """Thin, dependency-light client. Created per call site from settings."""

    def __init__(self, url: str, api_key: str, timeout: int = 30):
        self.base = _norm_base(url)
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        if not self.base or not self.api_key:
            raise SeerrError("Seerr URL and API key are required.")

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, follow_redirects=True)

    # -- health ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/api/v1/status", headers=_headers(self.api_key))
        except httpx.HTTPError as exc:
            raise SeerrError(f"Cannot reach Seerr at {self.base}: {exc}") from exc
        if r.status_code in (401, 403):
            raise SeerrError("Seerr rejected the API key (HTTP %s)." % r.status_code)
        if r.status_code != 200:
            raise SeerrError(f"Seerr /api/v1/status returned HTTP {r.status_code}.")
        try:
            return r.json()
        except ValueError as exc:
            raise SeerrError("Seerr returned a non-JSON status response.") from exc

    def test(self) -> tuple[bool, str]:
        try:
            data = self.status()
        except SeerrError as exc:
            return False, str(exc)
        version = (data.get("version") or "?")
        return True, f"Connected — {data.get('appName') or 'Seerr'} v{version}"

    # -- requests -------------------------------------------------------
    def create_request(
        self,
        *,
        tmdb_id: int | None,
        title: str,
        imdb_id: str | None = None,
        root_folder: str | None = None,
        quality_profile: str | None = None,
        tags: list[str] | None = None,
        search_on_add: bool | None = None,
        user_id: int | None = None,
    ) -> SeerrRequestResult:
        if not tmdb_id and not imdb_id:
            return SeerrRequestResult(
                ok=False, detail="No TMDB or IMDb id available for this movie."
            )

        payload: dict[str, Any] = {"mediaType": "movie", "title": title}
        if tmdb_id:
            payload["mediaId"] = int(tmdb_id)
        if root_folder:
            payload["rootFolderPath"] = root_folder
        if quality_profile:
            payload["qualityProfileId"] = _as_id(quality_profile)
        if tags:
            payload["tags"] = list(tags)
        if search_on_add is not None:
            payload["is4k"] = False
        if imdb_id and not tmdb_id:
            # Some forks accept imdbId for movie requests.
            payload["imdbId"] = imdb_id

        try:
            with self._client() as c:
                r = c.post(
                    f"{self.base}/api/v1/request",
                    headers={**_headers(self.api_key), "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            return SeerrRequestResult(ok=False, detail=f"Network error: {exc}")

        if r.status_code in (200, 201):
            try:
                data = r.json() or {}
            except ValueError:
                data = {}
            return SeerrRequestResult(
                ok=True,
                request_id=data.get("id"),
                status=data.get("status") or "pending",
                detail=(data.get("media", {}) or {}).get("title") or title,
            )

        if r.status_code == 409:
            return SeerrRequestResult(
                ok=True, status="already-requested", already=True, detail=title
            )

        detail = _error_detail(r)
        log.warning("Seerr rejected request for %s: HTTP %s %s", title, r.status_code, detail)
        return SeerrRequestResult(ok=False, detail=f"HTTP {r.status_code}: {detail}")

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/api/v1/request/{request_id}", headers=_headers(self.api_key))
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def list_requests(self, take: int = 100, skip: int = 0) -> list[dict[str, Any]]:
        try:
            with self._client() as c:
                r = c.get(
                    f"{self.base}/api/v1/request",
                    headers=_headers(self.api_key),
                    params={"take": take, "skip": skip, "filter": "all"},
                )
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        try:
            data = r.json() or {}
        except ValueError:
            return []
        results = data.get("results") if isinstance(data, dict) else data
        return results or []

    # -- root folders / profiles (for the settings UI dropdowns) -------
    def root_folders(self) -> list[dict[str, Any]]:
        """Jellyseerr exposes root folders under /api/v1/settings/plex; Overseerr
        under /api/v1/settings/overseerr. Try both."""
        for path in ("/api/v1/settings/plex", "/api/v1/settings/overseerr", "/api/v1/settings"):
            try:
                with self._client() as c:
                    r = c.get(f"{self.base}{path}", headers=_headers(self.api_key))
                if r.status_code != 200:
                    continue
                data = r.json() or {}
            except (httpx.HTTPError, ValueError):
                continue
            folders = None
            if isinstance(data, dict):
                for key in ("rootFolders", "rootFolderPaths"):
                    if isinstance(data.get(key), list):
                        folders = data[key]
                        break
            if folders:
                return [{"path": f} for f in folders]
        return []

    def quality_profiles(self) -> list[dict[str, Any]]:
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/api/v1/qualityprofile", headers=_headers(self.api_key))
            if r.status_code != 200:
                return []
            data = r.json() or {}
        except (httpx.HTTPError, ValueError):
            return []
        results = data.get("results") if isinstance(data, dict) else data
        return [
            {"id": p.get("id"), "name": p.get("name")}
            for p in (results or [])
            if p.get("id") is not None
        ]


def _as_id(value: Any) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _error_detail(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return (r.text or "")[:200]
    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            if data.get(key):
                return str(data[key])
        return str(data)[:200]
    return str(data)[:200]