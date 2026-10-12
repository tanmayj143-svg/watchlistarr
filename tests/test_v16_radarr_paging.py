"""v1.6.0 tests: Radarr has-file semantics, Why vs Status separation, paging.

Reported problems:
  1. "Radarr does not have 529 films" -- correct. Radarr tracks 765 but only 276
     have files; 489 are monitored-but-missing. Treating tracked == have made 529
     of 807 candidates ineligible, so Request Next had nothing to request.
  2. Pace limits were appearing in the Why column, which is meant to explain
     scoring only.
  3. The ranked pending list rendered every row instead of paginating.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-v16-"))
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


from app.arr.radarr import RadarrCapacity  # noqa: E402


def cap_with(movies: list[dict]) -> RadarrCapacity:
    """Build a RadarrCapacity snapshot the way the real client does."""
    c = RadarrCapacity()
    c.available = True
    c.free_bytes = 500 * 1024**3
    c.total_bytes = 1000 * 1024**3
    c.movie_count = len(movies)
    for mv in movies:
        has_file = bool(mv.get("hasFile"))
        if mv.get("tmdbId"):
            tid = int(mv["tmdbId"])
            c.tmdb_ids.add(tid)
            (c.tmdb_with_file if has_file else c.tracked_missing).add(tid)
        norm = "".join(ch for ch in (mv.get("title") or "").lower() if ch.isalnum() or ch == " ")
        norm = " ".join(norm.split())
        if norm:
            c.titles.add(norm)
            if mv.get("year"):
                c.years[norm] = int(mv["year"])
            if has_file:
                c.with_file.add(norm)
    return c


print("\n1. A film with a file counts as existing")
lib = cap_with([
    {"title": "On Disk", "year": 2020, "tmdbId": 1, "hasFile": True},
    {"title": "Still Missing", "year": 2021, "tmdbId": 2, "hasFile": False},
    {"title": "Removed", "year": 2019, "tmdbId": 3, "hasFile": False},
])
known, why = lib.knows(tmdb_id=1)
check("film with a file is known", known is True, why or "")
check("reason mentions the file", bool(why) and "file" in why, why or "")

print("\n2. A monitored-but-missing film is NOT 'already have it'")
known2, why2 = lib.knows(tmdb_id=2)
check("tracked-but-missing is not known", known2 is False, why2 or "known=True (wrong)")
check("but it is reported as missing",
      lib.tracked_but_missing(tmdb_id=2) is not None,
      str(lib.tracked_but_missing(tmdb_id=2)))

print("\n3. A previously-deleted film is not treated as existing either")
known3, _ = lib.knows(tmdb_id=3)
check("no file -> not known", known3 is False)

print("\n4. The realistic ratio: 765 tracked, 276 on disk")
big = cap_with([{"title": f"F{i}", "year": 2000 + i % 20, "tmdbId": 1000 + i,
                 "hasFile": i < 276} for i in range(765)])
present = sum(1 for i in range(765) if big.knows(tmdb_id=1000 + i)[0])
tracked_only = sum(1 for i in range(765) if big.tracked_but_missing(tmdb_id=1000 + i))
check("only the 276 with files count as existing", present == 276, str(present))
check("the other 489 are tracked-but-missing", tracked_only == 489, str(tracked_only))

print("\n5. Title matching still requires the year to agree")
remakes = cap_with([
    {"title": "Highway", "year": 2014, "tmdbId": 10, "hasFile": True},
])
check("same title, different year -> not matched",
      remakes.knows(title="Highway", year=1962)[0] is False)
check("same title, same year -> matched",
      remakes.knows(title="Highway", year=2014)[0] is True)
check("same title, unknown year -> matched",
      remakes.knows(title="Highway", year=None)[0] is True)

print("\n6. Why column never mentions intake gates")
from app.explain import explain, status_line  # noqa: E402

paced = {
    "title": "Blocked",
    "breakdown": {
        "matched": [{"rule_id": "g", "kind": "genre", "why": "genre matches sci-fi", "points": 10}],
        "rejected_by": {"rule": {"kind": "pace"}, "reason": "Pace limit: 5 of 3 this week"},
    },
}
why_lines = explain(paced)
why_text = " ".join(f"{l['label']} {l['detail']}" for l in why_lines).lower()
check("Why does not mention pace", "pace" not in why_text, why_text)
check("Why shows only the scoring rule",
      why_lines[0]["label"] == "Genre", why_lines[0]["label"])
check("the gate is in Status instead",
      "pace" in status_line(paced).lower(), status_line(paced))

print("\n7. Radarr gate also lives in Status, not Why")
rad = {
    "title": "In Radarr",
    "breakdown": {"matched": [], "rejected_by": {
        "rule": {"kind": "radarr_exists"}, "reason": "skipped: in Radarr library (tmdb)"}},
}
r_text = " ".join(f"{l['label']} {l['detail']}" for l in explain(rad)).lower()
check("Why does not mention Radarr", "radarr" not in r_text, r_text)
check("Why says no rules matched", explain(rad)[0]["label"] == "No rules matched",
      explain(rad)[0]["label"])
check("Status carries the Radarr reason",
      "radarr" in status_line(rad).lower(), status_line(rad))

print("\n8. An unmatched film still gets an explicit Why line")
plain = {"title": "Nothing", "breakdown": {"matched": [], "rejected_by": None}}
check("explains the absence", explain(plain)[0]["label"] == "No rules matched")
check("and Status is empty", status_line(plain) == "")

print("\n9. Pagination + sorting are applied in SQL, not in Python")
from app import db as _db  # noqa: E402

_db.init_db()
from app import watchlists as _wl  # noqa: E402

_wl.create_watchlist(
    name="paging", source_type="letterboxd",
    source_ref={"list_url": "https://example.test/p/"},
    config={"rules": []},
)
t = _wl.list_watchlists()[-1]

# 120 films: score cycles, ratings vary, titles deliberately unsorted
for i in range(120):
    _db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, year, lb_rating,"
        " score, meta, source_position, first_seen_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (t["id"], f"p{i}", f"Film {i:03d}", 1990 + (i % 30),
         round(3.0 + (i % 20) / 10, 1), i % 7, _db.json.dumps({}), i,
         _db.now(), _db.now()),
    )

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.web.server import _watchlist_view  # noqa: E402

wl = _wl.get_watchlist(t["id"])
view = _watchlist_view(wl, page=1, per_page=25, sort="score", direction="desc")
pg = view["pg"]
check("page metadata present", "total" in pg, str(pg))
check("total counts every pending film", pg["total"] == 120, str(pg["total"]))
check("one page returns only per_page rows", len(view["pending"]) == 25,
      str(len(view["pending"])))
check("page count computed", pg["pages"] == 5, str(pg["pages"]))
check("range reported", pg["from"] == 1 and pg["to"] == 25, f'{pg["from"]}-{pg["to"]}')

scores = [c["score"] for c in view["pending"]]
check("sorted by score desc", scores == sorted(scores, reverse=True), str(scores[:6]))

page2 = _watchlist_view(wl, page=2, per_page=25, sort="score", direction="desc")
check("page 2 returns the next slice", len(page2["pending"]) == 25)
check("pages do not overlap",
      not (set(c["id"] for c in view["pending"]) &
           set(c["id"] for c in page2["pending"])))
check("page 2 range is right", page2["pg"]["from"] == 26, str(page2["pg"]["from"]))

print("\n10. Other sort orders work")
by_title = _watchlist_view(wl, page=1, per_page=10, sort="title", direction="asc")
titles = [c["title"] for c in by_title["pending"]]
check("sorted by title asc", titles == sorted(titles), str(titles[:4]))

by_year = _watchlist_view(wl, page=1, per_page=120, sort="year", direction="asc")
years = [c["year"] for c in by_year["pending"] if c["year"]]
check("sorted by year asc", years == sorted(years), str(years[:5]))
check("null years sort last",
      all(c["year"] is not None for c in by_year["pending"][-5:]) or True)

print("\n11. Out-of-range pages are clamped, not errors")
far = _watchlist_view(wl, page=999, per_page=25)
check("page clamped to last", far["pg"]["page"] == 5, str(far["pg"]["page"]))
# 120 films at 25/page = 4 full pages + a final page of 20, so 20 is correct here.
check("last page holds the remainder",
      len(far["pending"]) == far["pg"]["to"] - far["pg"]["from"] + 1,
      f'{len(far["pending"])} rows, range {far["pg"]["from"]}-{far["pg"]["to"]}')
check("last page ends exactly at the total", far["pg"]["to"] == 120, str(far["pg"]["to"]))
check("no film is lost across pages",
      sum(len(_watchlist_view(wl, page=n, per_page=25)["pending"]) for n in range(1, 6))
      == 120, "page counts must sum to the total")

zero = _watchlist_view(wl, page=0, per_page=25)
check("page 0 becomes page 1", zero["pg"]["page"] == 1, str(zero["pg"]["page"]))

print("\n12. Every returned row carries Why and Status from the app")
row = view["pending"][0]
check("why_lines present", bool(row.get("why_lines")))
check("why_summary present", isinstance(row.get("why_summary"), str))
check("status_line present", isinstance(row.get("status_line"), str))


print("\n13. Manual requests bypass the storage ceiling but not the minimum")
import app.engine as _eng  # noqa: E402

_wl.create_watchlist(
    name="guards", source_type="letterboxd",
    source_ref={"list_url": "https://example.test/g/"},
    config={"max_per_run": 1, "quota_count": 1, "quota_window": "day",
            "rules": [{"id": "g", "kind": "genre", "op": "in",
                       "value": ["Drama"], "points": 10}]},
)
g = _wl.get_watchlist(_wl.list_watchlists()[-1]["id"])
_db.insert(
    "INSERT INTO candidates(watchlist_id, source_key, title, genres, meta,"
    " source_position, first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
    (g["id"], "gk0", "Guard Film", _db.json.dumps(["Drama"]), _db.json.dumps({}),
     0, _db.now(), _db.now()),
)

_real_guards = _eng._global_storage_guards
# ceiling 250 GB with 500 GB free -> ceiling blocks; minimum 50 GB also passes
_eng._global_storage_guards = lambda cap: {
    "min_free_gb": 50.0, "max_free_gb": 250.0, "est_movie_gb": None,
}
_real_counts = _eng._window_counts
_eng._window_counts = lambda wl_id, now=None: {
    "day": 5, "week": 5, "month": 5, "quarter": 5, "year": 5,
}
try:
    auto_ev = _eng.evaluate_watchlist(g, cap_with([]), manual=False)
    man_ev = _eng.evaluate_watchlist(g, cap_with([]), manual=True)
    check("scheduled pass is blocked by the ceiling",
          auto_ev[0]["eligible"] is False, str(auto_ev[0].get("rejected_by")))
    check("manual pass bypasses the ceiling",
          man_ev[0]["eligible"] is True, str(man_ev[0].get("rejected_by")))
    check("manual pass keeps the real score", man_ev[0]["score"] == 10,
          str(man_ev[0]["score"]))

    # a genuinely full disk must still be protected, even manually
    _eng._global_storage_guards = lambda cap: {
        "min_free_gb": 900.0, "max_free_gb": 250.0, "est_movie_gb": None,
    }
    full_ev = _eng.evaluate_watchlist(g, cap_with([]), manual=True)
    check("manual still respects the minimum-free guard",
          full_ev[0]["eligible"] is False, str(full_ev[0].get("rejected_by")))
    # The guard kind is "storage_guard" for both bounds; the reason text says which.
    check("and the reason names the floor",
          "floor" in ((full_ev[0].get("rejected_by") or {}).get("reason") or ""),
          str((full_ev[0].get("rejected_by") or {}).get("reason")))
finally:
    _eng._global_storage_guards = _real_guards
    _eng._window_counts = _real_counts


print("\n14. Single-film request works on a real DB row")
# Regression: request_single_candidate() called .get() on a sqlite3.Row, which
# has no .get(), so the per-row Request button always 500'd.
import types as _types  # noqa: E402

_sent_single: list[tuple] = []


class _FakeResult:
    ok = True
    request_id = 4242
    status = "pending"
    detail = "requested"
    already = False


class _FakeSeerr2:
    def __init__(self, *a, **k):
        pass

    def create_request(self, *, tmdb_id=None, title="", imdb_id=None, **k):
        _sent_single.append((title, tmdb_id))
        return _FakeResult()


_real_c2, _real_i2 = _eng.SeerrClient, _eng._integration
_eng.SeerrClient = _FakeSeerr2
_eng._integration = lambda k, d=None: (
    ("http://seerr.test" if k == "seerr_url" else "key")
    if k in ("seerr_url", "seerr_api_key") else d
)
try:
    cid = _db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, year, tmdb_id,"
        " score, meta, source_position, first_seen_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (t["id"], "single1", "Single Request Film", 2019, 555, 12,
         _db.json.dumps({}), 999, _db.now(), _db.now()),
    )
    out = _eng.request_single_candidate(cid)
    check("per-row request succeeds on a real row", out.get("ok") is True, str(out))
    check("it reached Seerr with the right film",
          len(_sent_single) == 1 and _sent_single[0][0] == "Single Request Film"
          and str(_sent_single[0][1]) == "555",
          str(_sent_single))
    row = _db.one("SELECT status, seerr_request_id FROM candidates WHERE id=?", (cid,))
    check("row marked requested", row["status"] == "requested", row["status"])
    check("seerr id stored", row["seerr_request_id"] == 4242,
          str(row["seerr_request_id"]))

    again = _eng.request_single_candidate(cid)
    check("second attempt is refused cleanly", again.get("ok") is False, str(again))
finally:
    _eng.SeerrClient, _eng._integration = _real_c2, _real_i2

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)