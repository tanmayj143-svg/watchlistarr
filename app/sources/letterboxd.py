"""Letterboxd source adapter — list pages and member watchlists.

Free and login-free. Verified against live Letterboxd during v1.0.0 development:

* ``https://letterboxd.com/<user>/list/<slug>/``      -> 100 items per page
* ``https://letterboxd.com/<user>/list/<slug>/page/2/`` -> pagination works
* ``https://letterboxd.com/<user>/watchlist/``          -> member watchlist
* each poster carries ``data-item-slug`` / ``data-item-name`` ("Title (Year)")

The RSS feeds are behind a Cloudflare challenge and are deliberately not used;
the HTML parse is the reliable path. Letterboxd slugs are canonical and stable,
so they double as the dedup key.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
from urllib.parse import quote

import httpx

from ..config import settings
from .base import SourceError, SourceItem, normalise_ref

_ITEM_RE = re.compile(
    r'<div class="react-component"[^>]*?data-item-slug="(?P<slug>[a-z0-9-]+)"'
    r'[^>]*?data-item-name="(?P<name>[^"]*)"',
    re.S,
)
# Fallback: some templates put the name attribute before the slug.
_ITEM_REV_RE = re.compile(
    r'data-item-name="(?P<name>[^"]*)"[^>]*?data-item-slug="(?P<slug>[a-z0-9-]+)"',
    re.S,
)
_SLUG_ONLY_RE = re.compile(r'data-item-slug="([a-z0-9-]+)"')
_NAME_YEAR_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*$")
# The /lists/ index renders relative hrefs, so match both forms.
_LIST_LINK_RE = re.compile(r'href="(/[A-Za-z0-9_-]+/list/[A-Za-z0-9_-]+/)"')
_LIST_NAV_TAIL = {"edit", "likes", "comments", "watchlist"}
# Letterboxd URL shapes people actually paste:
#   /user/list/slug/ -> a named list
#   /user/watchlist/ -> the member's watchlist
#   /user/films/ , /user/likes/ -> their public collection
# Trailing /page/N/ and any ?query are ignored.
_WATCHLIST_ALIASES = {"watchlist", "films", "likes", "favorites", "diary"}
_URL_RE = re.compile(
    r"^https?://(?:www\.)?letterboxd\.com/(?P<user>[A-Za-z0-9_.-]+)"
    # either /<user>/list/<slug>/  or  /<user>/<collection>/
    r"(?:/(?:list)/(?P<slug>[A-Za-z0-9_.-]+)"
    r"|/(?P<kind>[A-Za-z0-9_.-]+))?"
    r"(?:/page/\d+)?/?(?:[?#].*)?$",
    re.I,
)

PER_PAGE = 100

log = logging.getLogger("watchlistarr.letterboxd")


def parse_list_url(url: str) -> tuple[str, str | None] | None:
    """Return (username, list_slug) from a pasted Letterboxd URL.

    `slug` is None for a member-watchlist-style URL. Returns None only when the
    value is not a Letterboxd profile/list URL, so the caller can explain the
    accepted shapes rather than silently failing.
    """
    m = _URL_RE.match(url.strip())
    if not m:
        return None
    user = m.group("user")
    slug = m.group("slug")
    kind = (m.group("kind") or "").lower()

    if slug:                      # /user/list/slug/  -> named list
        return user, slug
    if kind in _WATCHLIST_ALIASES:  # /user/watchlist/ etc
        return user, None
    if not kind:                  # bare profile /user/
        return user, None
    return None


class LetterboxdSource:
    key_prefix = "lb"
    label = "Letterboxd"

    def __init__(self, source_ref: dict):
        self.ref = normalise_ref(source_ref)
        self.user: str | None = None
        self.slug: str | None = None  # None => the member watchlist
        self._resolve()

    # -- ref resolution -------------------------------------------------
    def _resolve(self) -> None:
        ref = self.ref
        if ref.get("list_url"):
            parsed = parse_list_url(ref["list_url"])
            if not parsed:
                raise SourceError(
                    f"Cannot read that Letterboxd URL: {ref['list_url']}. "
                    "Use https://letterboxd.com/<user>/list/<slug>/ for a list, "
                    "or https://letterboxd.com/<user>/watchlist/ for their watchlist."
                )
            self.user, self.slug = parsed
            return
        if ref.get("username") and ref.get("list_slug"):
            self.user, self.slug = ref["username"], ref["list_slug"]
            return
        if ref.get("username"):  # member watchlist
            self.user, self.slug = ref["username"], None
            return
        raise SourceError("Letterboxd needs a username and list slug (or a full list URL).")

    @property
    def base_url(self) -> str:
        if self.slug:
            return f"https://letterboxd.com/{self.user}/list/{self.slug}/"
        return f"https://letterboxd.com/{self.user}/watchlist/"

    @property
    def watchlist_url(self) -> str:
        return self.base_url

    # -- fetching -------------------------------------------------------
    def _page_url(self, page: int) -> str:
        if page == 1:
            return self.base_url
        return f"{self.base_url}page/{page}/"

    def fetch(self) -> list[SourceItem]:
        # Long watchlists run to dozens of pages; the old default of 10 would
        # stop early. Raise the ceiling and let Letterboxd end the walk.
        max_pages = max(settings.max_pages_per_list, 120)
        limit = self.ref.get("max_items") or (PER_PAGE * max_pages)
        items: list[SourceItem] = []
        seen: set[str] = set()

        with httpx.Client(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            for page in range(1, max_pages + 1):
                url = self._page_url(page)
                try:
                    resp = client.get(url)
                except httpx.HTTPError as exc:  # network level
                    if page == 1:
                        raise SourceError(f"Letterboxd unreachable: {exc}") from exc
                    break
                if resp.status_code in (403, 429):
                    if page == 1:
                        raise SourceError(
                            f"Letterboxd returned HTTP {resp.status_code} for {url}. "
                            "This usually means Cloudflare rate-limiting this IP — try again later "
                            "or lower the sync frequency."
                        )
                    break
                if resp.status_code == 404:
                    if page == 1:
                        raise SourceError(f"Letterboxd list not found: {url}")
                    break
                resp.raise_for_status()

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
                if new == 0:
                    break
                if len(items) >= limit:
                    break
                # Only stop when Letterboxd says there is no next page.
                if page >= max_pages:
                    break
                if not self.has_next_page(resp.text):
                    log.info("letterboxd: no further pages after page %s (%s)", page, url)
                    break

        return items[:limit]

    @staticmethod
    def has_next_page(html: str) -> bool:
        """True when Letterboxd advertises an "Older" link to a further page.

        Page size is NOT a reliable stop signal: named lists render 100 items per
        page while a member watchlist renders only 28, so the old
        `len(page_items) < 100` check treated page 1 of any watchlist as the last
        page and silently returned 28 of 812 films. Ask the page instead.
        """
        if re.search(r'class="next"\s+href="([^"]+)"', html):
            return True
        # Fallback: any explicit /page/N/ link with N >= 2 means more to fetch.
        return bool(re.search(r'href="[^"]*/page/([2-9]\d*)/"', html))

    # -- parsing --------------------------------------------------------
    @staticmethod
    def parse(page_html: str) -> list[SourceItem]:
        pairs: list[tuple[str, str]] = []
        for m in _ITEM_RE.finditer(page_html):
            pairs.append((m.group("slug"), m.group("name")))
        if not pairs:
            for m in _ITEM_REV_RE.finditer(page_html):
                pairs.append((m.group("slug"), m.group("name")))
        if not pairs:  # last resort: slugs only, names come from enrichment
            pairs = [(s, "") for s in _SLUG_ONLY_RE.findall(page_html)]

        out: list[SourceItem] = []
        for idx, (slug, name) in enumerate(pairs):
            clean = html_lib.unescape(name or "").strip()
            title, year = _split_name(clean)
            if not title:
                title = slug.replace("-", " ").title()
            out.append(
                SourceItem(
                    source_key=f"lb:{slug}",
                    title=title,
                    year=year,
                    slug=slug,
                    meta={"position": idx},
                )
            )
        return out


def _split_name(name: str) -> tuple[str, int | None]:
    m = _NAME_YEAR_RE.match(name)
    if m:
        try:
            return m.group("title").strip(), int(m.group("year"))
        except ValueError:
            return m.group("title").strip(), None
    return name.strip(), None


def discover_lists(user: str, limit: int = 100) -> list[dict[str, str]]:
    """List every public list owned by `user` (used by the add-watchlist UI).

    Verified live: /<user>/lists/ returns relative hrefs plus nav links
    (/edit/, /likes/, /comments/) that must be filtered out.
    """
    url = f"https://letterboxd.com/{quote(user)}/lists/"
    with httpx.Client(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        resp = client.get(url)
        if resp.status_code == 404:
            raise SourceError(f"No Letterboxd user called '{user}' (HTTP 404).")
        if resp.status_code != 200:
            raise SourceError(f"Letterboxd returned HTTP {resp.status_code} for {url}")

    found: list[str] = []
    seen: set[str] = set()
    for path in _LIST_LINK_RE.findall(resp.text):
        if path.rstrip("/").split("/")[-1] in _LIST_NAV_TAIL:
            continue
        if path in seen:
            continue
        seen.add(path)
        found.append(path)

    return [
        {
            "list_url": f"https://letterboxd.com{path}",
            "username": path.split("/")[1],
            "list_slug": path.split("/list/")[1].strip("/"),
        }
        for path in found
    ][:limit]