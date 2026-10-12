"""Shared types for source adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class SourceError(RuntimeError):
    """Raised when a source cannot be fetched or parsed."""


@dataclass
class SourceItem:
    """One movie as advertised by the source, before enrichment."""

    source_key: str
    title: str
    year: int | None = None
    slug: str | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    poster: str | None = None
    position: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "title": self.title,
            "year": self.year,
            "slug": self.slug,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "poster": self.poster,
            "position": self.position,
            "meta": self.meta,
        }


class SourceAdapter(Protocol):
    """Anything that can turn a watchlist reference into SourceItems."""

    #: stable key used in candidate dedup
    key_prefix: str

    def fetch(self) -> list[SourceItem]:  # pragma: no cover - protocol
        ...


def normalise_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """Trim/validate a watchlist's source_ref dict."""
    out: dict[str, Any] = {}
    for key in ("username", "list_slug", "list_url", "list_id"):
        val = (ref.get(key) or "").strip()
        if val:
            out[key] = val
    if "list_url" in out and not out["list_url"].startswith("http"):
        out["list_url"] = "https://" + out["list_url"].lstrip("/")
    limit = ref.get("max_items")
    if isinstance(limit, (int, float)) and limit > 0:
        out["max_items"] = int(limit)
    return out