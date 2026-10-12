"""Pagination regression tests (v1.3.4).

Reported symptom: a member watchlist with 29 pages (~800 films) synced only the
first 28.

Root cause: `fetch()` stopped when `len(page_items) < PER_PAGE` (100). Named
lists do render 100 items per page, but a member watchlist renders only 28, so
page 1 was always mistaken for the last page. The fix asks Letterboxd whether a
next page exists rather than inferring it from page size.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-page-"))
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sources.letterboxd import LetterboxdSource  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


PAGE_MID = """
<ul class="poster-list">
  <li class="posteritem" data-item-slug="film-one" data-item-name="Film One (2001)"></li>
  <li class="posteritem" data-item-slug="film-two" data-item-name="Film Two (2002)"></li>
</ul>
<div class="pagination"><div class="paginate-nextprev"><span class="previous">Newer</span></div>
<div class="paginate-nextprev"><a class="next" href="/u/watchlist/page/3/">Older</a></div>
<div class="paginate-pages"><ul><li><a href="/u/watchlist/page/2/">2</a></li></ul></div></div>
"""

PAGE_LAST = """
<ul class="poster-list">
  <li class="posteritem" data-item-slug="film-nine" data-item-name="Film Nine (2009)"></li>
</ul>
<div class="pagination"><div class="paginate-nextprev"><span class="previous">Newer</span></div>
<div class="paginate-nextprev paginate-disabled"><span class="next">Older</span></div>
<div class="paginate-pages"><ul><li><span class="paginate-current">9</span></li></ul></div></div>
"""

PAGE_FIRST_NO_NEXT = """
<ul class="poster-list">
  <li class="posteritem" data-item-slug="only-film" data-item-name="Only Film (2000)"></li>
</ul>
<div class="pagination"><div class="paginate-nextprev paginate-disabled"><span class="previous">Newer</span></div>
<div class="paginate-nextprev paginate-disabled"><span class="next">Older</span></div></div>
"""

print("\n1. next-page detection (the regression)")
check("page with an Older link -> has next", LetterboxdSource.has_next_page(PAGE_MID))
check("final page (next disabled) -> no next", not LetterboxdSource.has_next_page(PAGE_LAST))
check("single page, both disabled -> no next",
      not LetterboxdSource.has_next_page(PAGE_FIRST_NO_NEXT))
check("empty html -> no next", not LetterboxdSource.has_next_page(""))


print("\n2. The old heuristic would have failed here")
PER_PAGE = 100
items_on_watchlist_page = len(LetterboxdSource.parse(PAGE_MID))
check("a watchlist page holds fewer items than PER_PAGE",
      items_on_watchlist_page < PER_PAGE,
      f"{items_on_watchlist_page} < {PER_PAGE}")
check("...yet there IS a next page, so size alone is not a valid stop signal",
      LetterboxdSource.has_next_page(PAGE_MID))


print("\n3. Live: the 29-page watchlist paginates fully")
src = LetterboxdSource(
    {"list_url": "https://letterboxd.com/scion_of_societ/watchlist/", "max_items": 120}
)
items = src.fetch()
slugs = {i.source_key for i in items}
check("fetched well past one page", len(items) > 28, f"{len(items)} items")
check("no duplicate films across pages", len(slugs) == len(items),
      f"{len(slugs)} unique of {len(items)}")
check("max_items respected", len(items) <= 120, str(len(items)))

# Positions must be unique per page, proving later pages were walked.
positions = [i.meta.get("position") for i in items]
check("every item carries a page position", all(p is not None for p in positions))
check("positions repeat across pages (28 per page)",
      len(set(positions)) < len(positions),
      f"{len(set(positions))} distinct position values across {len(positions)} items")


print("\n4. Live: page 1 and page 2 do not overlap")
import httpx  # noqa: E402

from app.config import settings as _cfg  # noqa: E402

with httpx.Client(timeout=30, follow_redirects=True,
                  headers={"User-Agent": _cfg.user_agent}) as c:
    a = LetterboxdSource.parse(c.get("https://letterboxd.com/scion_of_societ/watchlist/").text)
    b = LetterboxdSource.parse(
        c.get("https://letterboxd.com/scion_of_societ/watchlist/page/2/").text)
sa = {x.source_key for x in a}
sb = {x.source_key for x in b}
check("page 1 has 28", len(sa) == 28, str(len(sa)))
check("page 2 has 28", len(sb) == 28, str(len(sb)))
check("zero overlap between page 1 and page 2", not (sa & sb), str(len(sa & sb)))

print("\n5. Sync must survive films whose metadata cannot be resolved")
# Regression: a large sync crashed with AttributeError: 'NoneType' object has no
# attribute 'year' when enrich() returned None for an obscure title.
import tempfile as _tf  # noqa: E402

os.environ["WATCHLISTARR_DATA"] = _tf.mkdtemp(prefix="wlarr-page2-")
for mod in [m for m in list(sys.modules) if m.startswith("app.")]:
    if mod.endswith(("db", "engine", "settings_service", "watchlists")):
        del sys.modules[mod]

from app import db as _db  # noqa: E402
from app import engine as _eng  # noqa: E402
from app import watchlists as _wl  # noqa: E402

_db.init_db()
_wl.create_watchlist(
    name="unresolvable", source_type="letterboxd",
    source_ref={"list_url": "https://letterboxd.com/scion_of_societ/watchlist/"},
    config={"max_per_run": 1, "rules": []},
)
u = _wl.list_watchlists()[-1]

from app.sources.base import SourceItem  # noqa: E402

class _Stub(LetterboxdSource):
    """Feeds three films, one with no resolvable metadata."""
    def __init__(self, ref):
        self.ref, self.user, self.slug = ref, "x", "y"
        self._items = [
            SourceItem(source_key="lb:ok1", title="Resolvable Film", year=2001,
                       slug="ok1", position=0),
            SourceItem(source_key="lb:hard", title="Zzqx Unresolvable Title Xx", year=None,
                       slug="hard", position=1),
            SourceItem(source_key="lb:ok2", title="Second Film", year=2002,
                       slug="ok2", position=2),
        ]
    def fetch(self):
        return list(self._items)

import app.engine as _eng_mod  # noqa: E402
_real_build = _eng_mod.build_source
_eng_mod.build_source = lambda t, r: _Stub(r)

_real_resolve = _eng_mod.resolve_meta
_calls = {"n": 0}
def _flaky(*a, **k):
    _calls["n"] += 1
    return None if _calls["n"] == 2 else _real_resolve(*a, **k)
_eng_mod.resolve_meta = _flaky

try:
    stats = _eng.sync_watchlist(_wl.get_watchlist(u["id"]), enrich_limit=3)
    check("sync completes despite an unresolvable film", stats.get("ok") is True, str(stats)[:90])
    check("all three films stored", stats.get("new") == 3, str(stats.get("new")))
    rows = _db.query("SELECT title, year, released, source_position FROM candidates "
                     "WHERE watchlist_id=?", (u["id"],))
    check("the unresolvable film is kept, not dropped", len(rows) == 3, str(len(rows)))
    check("positions recorded", all(r["source_position"] is not None for r in rows))
    check("no row has a year it should not",
          any(r["title"].startswith("Zzqx") and r["year"] is None for r in rows),
          str([(r["title"][:22], r["year"]) for r in rows]))
finally:
    _eng_mod.build_source = _real_build
    _eng_mod.resolve_meta = _real_resolve

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)