"""Letterboxd URL parsing tests (v1.2.1).

The reported bug: pasting https://letterboxd.com/<user>/watchlist/ was rejected
with "Expected .../<user>/list/<slug>/", even though that is a perfectly valid
Letterboxd URL and the adapter already supported it via `username`.

Run: python tests/test_letterboxd_urls.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-url-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sources.base import SourceError  # noqa: E402
from app.sources.letterboxd import LetterboxdSource, parse_list_url  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


print("\n1. The reported URL now parses")
got = parse_list_url("https://letterboxd.com/scion_of_societ/watchlist/")
check("member watchlist URL parses", got == ("scion_of_societ", None), str(got))

try:
    src = LetterboxdSource({"list_url": "https://letterboxd.com/scion_of_societ/watchlist/"})
    check("adapter accepts it", src.user == "scion_of_societ" and src.slug is None,
          f"user={src.user} slug={src.slug}")
    check("resolves to the watchlist URL",
          src.base_url == "https://letterboxd.com/scion_of_societ/watchlist/",
          src.base_url)
except SourceError as exc:
    check("adapter accepts it", False, str(exc))


print("\n2. Named lists still work")
CASES = [
    ("https://letterboxd.com/kaitienk/list/chick-flicks/", ("kaitienk", "chick-flicks")),
    ("https://letterboxd.com/kaitienk/list/chick-flicks", ("kaitienk", "chick-flicks")),
    ("https://letterboxd.com/kaitienk/list/chick-flicks/page/2/", ("kaitienk", "chick-flicks")),
    ("https://letterboxd.com/kaitienk/list/chick-flicks/?sort=list", ("kaitienk", "chick-flicks")),
    ("http://letterboxd.com/dave/list/best-of-2025/", ("dave", "best-of-2025")),
    ("https://www.letterboxd.com/dave/list/best-of-2025/", ("dave", "best-of-2025")),
    # member collection shapes
    ("https://letterboxd.com/dave/watchlist/", ("dave", None)),
    ("https://letterboxd.com/dave/films/", ("dave", None)),
    ("https://letterboxd.com/dave/likes/", ("dave", None)),
    ("https://letterboxd.com/dave/watchlist", ("dave", None)),
    ("https://letterboxd.com/dave/", ("dave", None)),
    # slugs with dots/underscores/dashes
    ("https://letterboxd.com/official/list/letterboxds-top-500-films/",
     ("official", "letterboxds-top-500-films")),
    ("https://letterboxd.com/dave/list/best_of_2025/", ("dave", "best_of_2025")),
]
for url, want in CASES:
    check(f"parses {url[:56]}", parse_list_url(url) == want, str(parse_list_url(url)))


print("\n3. Genuinely wrong URLs are still rejected (with a clear message)")
for bad in [
    "https://imdb.com/list/ls012733158/",
    "https://letterboxd.com/film/fight-club/",
    "https://letterboxd.com/dave/list/",
    "not a url at all",
    "",
]:
    check(f"rejects {bad[:40]!r}", parse_list_url(bad) is None, str(parse_list_url(bad)))

try:
    LetterboxdSource({"list_url": "https://imdb.com/list/ls012733158/"})
    check("adapter raises on a non-Letterboxd URL", False)
except SourceError as exc:
    msg = str(exc)
    check("adapter raises on a non-Letterboxd URL", True)
    check("error names both accepted shapes",
          "/list/" in msg and "watchlist" in msg, msg[:110])

# a list URL with no slug is not a list; it must not silently become a watchlist
try:
    LetterboxdSource({"list_url": "https://letterboxd.com/dave/list/"})
    check("empty list slug rejected", False)
except SourceError:
    check("empty list slug rejected", True)


print("\n4. username-only form is unchanged")
try:
    src = LetterboxdSource({"username": "scion_of_societ"})
    check("username form still works", src.base_url.endswith("/watchlist/"), src.base_url)
except SourceError as exc:
    check("username form still works", False, str(exc))


print("\n5. Fetch builds the right URL")
try:
    src = LetterboxdSource({"list_url": "https://letterboxd.com/scion_of_societ/watchlist/"})
    p1 = src._page_url(1)
    check("page 1 is the watchlist URL",
          p1 == "https://letterboxd.com/scion_of_societ/watchlist/", p1)
    check("pagination target derived from it",
          src._page_url(2) == "https://letterboxd.com/scion_of_societ/watchlist/page/2/",
          src._page_url(2))
except Exception as exc:
    check("fetch builds the right URL", False, str(exc))

lst = LetterboxdSource({"list_url": "https://letterboxd.com/kaitienk/list/chick-flicks/"})
check("named list pagination",
      lst._page_url(3) == "https://letterboxd.com/kaitienk/list/chick-flicks/page/3/",
      lst._page_url(3))

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)