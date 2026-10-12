"""Source adapters: fetch raw movie entries from a watchlist.

Every adapter returns a list of `SourceItem` — a minimal "we know this exists"
record. Enrichment (genres/country/year) happens later in `metadata`, because
the list pages carry almost no metadata while scoring needs all of it.
"""
from .base import SourceItem, SourceError
from .letterboxd import LetterboxdSource
from .imdb_list import IMDbListSource

REGISTRY: dict[str, type] = {
    "letterboxd": LetterboxdSource,
    "imdb_list": IMDbListSource,
}

LABELS: dict[str, str] = {
    "letterboxd": "Letterboxd",
    "imdb_list": "IMDb List",
}


def build(source_type: str, source_ref: dict) -> "SourceAdapter":
    """Instantiate the adapter for `source_type`."""
    if source_type not in REGISTRY:
        raise SourceError(f"Unknown source type: {source_type}")
    return REGISTRY[source_type](source_ref)  # type: ignore[operator]


__all__ = [
    "SourceItem",
    "SourceError",
    "LetterboxdSource",
    "IMDbListSource",
    "REGISTRY",
    "LABELS",
    "build",
]