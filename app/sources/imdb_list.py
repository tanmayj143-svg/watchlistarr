"""IMDb *list* source adapter (EXPERIMENTAL).

IMDb list pages are protected by bot mitigation: during v1.0.0 development the
HTML endpoint answered HTTP 202 with a JS challenge stub and the public GraphQL
endpoint returned 403, from a plain datacenter IP. This adapter therefore tries,
in order:

1. the list's own ``?format=json``-style helpers are not public, so it parses the
   rendered HTML for ``/title/ttNNNNNNN/`` links;
2. ``https://v3.sg.media-imdb.com/suggestion/...`` for id/title/poster backfill.

It is shipped because the parse works whenever IMDb serves real HTML (residential
IPs, some cloud exits), and it fails *loudly* rather than silently. Letterboxd is
the recommended free source; see docs/SOURCES.md.
"""
from __future__ import annotations

import re

import httpx

from ..config import settings
from .base import SourceError, SourceItem, normalise_ref

_TT_RE = re.compile(r"/title/(tt\d{7,9})[/\"]")
_NAME_YEAR_RE = re.compile(
    r'"(?:nameText":\{"text":"|alt=")(?P<title>[^"]{1,200}?)(?:",\s*"\d{4}"|\s*\(\d{4}\))'
)
_LIST_ID_RE = re.compile(r"ls\d{6,}")
PER_PAGE = 100


class IMDbListSource:
    key_prefix = "imdb"
    label = "IMDb List"

    def __init__(self, source_ref: dict):
        self.ref = normalise_ref(source_ref)
        raw = self.ref.get("list_url") or ""
        found = _LIST_ID_RE.search(raw)
        if not found:
            raise SourceError(
                "IMDb source needs the list URL or id, e.g. https://www.imdb.com/list/ls012733158/"
            )
        self.list_id = found.group(0)

    @property
    def base_url(self) -> str:
        return f"https://www.imdb.com/list/{self.list_id}/"

    def fetch(self) -> list[SourceItem]:
        limit = self.ref.get("max_items") or (PER_PAGE * settings.max_pages_per_list)
        items: list[SourceItem] = []
        seen: set[str] = set()
        with httpx.Client(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ) as client:
            for page in range(1, settings.max_pages_per_list + 1):
                url = self.base_url if page == 1 else f"{self.base_url}?page={page}"
                try:
                    resp = client.get(url)
                except httpx.HTTPError as exc:
                    if page == 1:
                        raise SourceError(f"IMDb unreachable: {exc}") from exc
                    break
                if resp.status_code != 200:
                    if page == 1:
                        raise SourceError(
                            f"IMDb returned HTTP {resp.status_code} for {url}. IMDb lists sit "
                            "behind bot protection — this is expected from many datacenter IPs. "
                            "Use Letterboxd as the source, or supply an optional proxy in Settings."
                        )
                    break
                page_items = self.parse(resp.text)
                if not page_items:
                    break
                new = 0
                for it in page_items:
                    if it.source_key in seen:
                        continue
                    seen.add(it.source_key)
                    items.append(it)
                    new += 1
                if len(page_items) < PER_PAGE or new == 0 or len(items) >= limit:
                    break
        return items[:limit]

    @staticmethod
    def parse(page_html: str) -> list[SourceItem]:
        # title links, preserving document order, de-duplicated
        ordered: list[str] = []
        seen: set[str] = set()
        for m in _TT_RE.finditer(page_html):
            tt = m.group(1)
            if tt in seen:
                continue
            seen.add(tt)
            ordered.append(tt)
        titles = {t.lower(): t for t in _NAME_YEAR_RE.findall(page_html)}
        out: list[SourceItem] = []
        for idx, tt in enumerate(ordered):
            # the id alone is a fine key; title gets refined during enrichment
            out.append(
                SourceItem(
                    source_key=f"imdb:{tt}",
                    title=tt,
                    imdb_id=tt,
                    meta={"position": idx, "titles_hint": len(titles)},
                )
            )
        return out