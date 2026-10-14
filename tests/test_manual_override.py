"""Manual-override and metadata-backfill tests (v1.3.9).

Two bugs fixed here:

1. "Request Next" was calling the same endpoint as the scheduler, so a manual
   click was silently throttled by the Pace valve the operator was trying to
   override.

2. Enrichment selected only items with no existing row. Films ingested while the
   per-sync budget was exhausted were stored with no genre, country, language or
   ids. On a 29-page import that left 659 of 807 rows effectively blank, so
   genre and country rules matched almost nothing.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-manual-"))
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


from app import db as _db  # noqa: E402
from app import engine as _eng  # noqa: E402
from app import settings_service as _svc  # noqa: E402
from app import watchlists as _wl  # noqa: E402
from app.arr.radarr import RadarrCapacity  # noqa: E402

_db.init_db()
_svc.set_integration("filter_unreleased", False)
_svc.set_integration("ignore_radarr_existing", False)
_svc.set_integration("tmdb_api_key", "")


def cap() -> RadarrCapacity:
    c = RadarrCapacity()
    c.available = True
    c.free_bytes = 500 * 1024**3
    c.total_bytes = 1000 * 1024**3
    c.movie_count = 100
    return c


_wl.create_watchlist(
    name="manual-override", source_type="letterboxd",
    source_ref={"list_url": "https://example.test/y/"},
    config={
        "max_per_run": 1,
        # Pace: 1 allowed per week, and we pretend it is already spent, so the
        # scheduled valve is definitely closed.
        "quota_count": 1,
        "quota_window": "week",
        "rules": [{"id": "lb", "kind": "letterboxd_rating", "op": "gte",
                   "value": 4, "points": 20}],
    },
)
t = _wl.list_watchlists()[-1]

for i, (title, lb) in enumerate([("Top Film", 4.5), ("Runner Up", 4.2)]):
    # Fully populated, so the backfill must skip them.
    _db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, lb_rating,"
        " imdb_id, tmdb_id, genres, countries, languages, meta,"
        " source_position, first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["id"], f"k{i}", title, lb, f"tt000000{i}", str(100 + i),
         _db.json.dumps(["Drama"]), _db.json.dumps(["India"]),
         _db.json.dumps(["en"]), _db.json.dumps({"unresolved": False}),
         i, _db.now(), _db.now()),
    )

_real_counts = _eng._window_counts
_eng._window_counts = lambda wl_id, now=None: {
    "day": 1, "week": 1, "month": 1, "quarter": 1, "year": 1,
}

print("\n1. A manual request ignores the Pace valve")
wl = _wl.get_watchlist(t["id"])
auto = _eng.evaluate_watchlist(wl, cap(), manual=False)
manual = _eng.evaluate_watchlist(wl, cap(), manual=True)
check("scheduled pass blocks everything (quota is 0/week)",
      all(not e["eligible"] for e in auto),
      str([e["eligible"] for e in auto]))
check("scheduled pass reports pace as the blocker",
      all((e["rejected_by"] or {}).get("rule", {}).get("kind") == "pace" for e in auto))
check("manual pass makes the top film eligible", manual[0]["eligible"] is True,
      f'{manual[0]["title"]} eligible={manual[0]["eligible"]}')
check("manual pass blocks nothing on pace",
      not any((e["rejected_by"] or {}).get("rule", {}).get("kind") == "pace" for e in manual))

print("\n2. Manual override changes eligibility only, never the score")
for a, b in zip(auto, manual, strict=False):
    check(f'{a["title"]}: score unchanged ({a["score"]} == {b["score"]})',
          a["score"] == b["score"])
check("ranking is identical in both passes",
      [e["title"] for e in auto] == [e["title"] for e in manual])

print("\n3. manual= forces exactly one request regardless of the per-run cap")
import app.engine as _eng_mod  # noqa: E402

_sent: list[str] = []


from app.arr.seerr import SeerrRequestResult as _SeerrResult  # noqa: E402


class _FakeSeerr:
    def __init__(self, *a, **k):
        pass

    def create_request(self, *a, **k):
        payload = a[0] if a else k
        title = ""
        if isinstance(payload, dict):
            title = str(payload.get("title") or "")
            if not title:
                movie = payload.get("movie") or {}
                title = str(movie.get("title") or "") if isinstance(movie, dict) else ""
            # Also catch tmdb/imdb id fields so we can assert on the right film.
            _sent.append(title or str(payload.get("tmdbId") or payload.get("tmdb_id") or ""))
        return _SeerrResult(ok=True, request_id=1, status="pending",
                            detail="requested", already=False)

    def exists(self, **k):
        return False


_orig_client = _eng.SeerrClient
_orig_int = _eng._integration


def _fake_int(key, default=None):
    if key in ("seerr_url", "seerr_api_key"):
        return "http://seerr.test" if key == "seerr_url" else "k"
    if key == "tmdb_api_key":
        # v1.7.0: metadata is TMDB-only, so the backfill needs a key to run.
        return "test-key"
    return _orig_int(key, default)


_eng.SeerrClient = _FakeSeerr
_eng._integration = _fake_int

try:
    res = _eng.request_for_watchlist(_wl.get_watchlist(t["id"]), cap(), manual=True)
    check("manual click requested a film despite quota 0", res.get("requested") == 1,
          str(res))
    check("it requested the top-scored film", _sent and _sent[0] == "Top Film", str(_sent))

    _sent.clear()
    res2 = _eng.request_for_watchlist(_wl.get_watchlist(t["id"]), cap(), manual=False)
    check("scheduled run still respects the valve and requests nothing",
          res2.get("requested") == 0, str(res2.get("requested")))
finally:
    _eng.SeerrClient = _orig_client
    _eng._integration = _orig_int
    _eng._window_counts = _real_counts

print("\n4. Metadata backfill only touches films missing metadata")
# v1.7.0: metadata is TMDB-only, so the backfill requires a key.
_svc.set_integration("tmdb_api_key", "test-key")
now = _db.now()
for i, title in enumerate(["No Genres", "Has Genres"]):
    genres = "[]" if title == "No Genres" else _db.json.dumps(["Drama"])
    meta = {"unresolved": True} if title == "No Genres" else {"unresolved": False}
    _db.insert(
        "INSERT INTO candidates(watchlist_id, source_key, title, slug, genres,"
        " meta, source_position, first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (t["id"], f"bf{i}", title, f"slug-{i}", genres, _db.json.dumps(meta), i, now, now),
    )

seen: list[str] = []


class _FakeMeta:
    def __init__(self, title):
        self.title = title
        self.release_year = 2001
        self.imdb_id = "tt0000001"
        self.tmdb_id = "1"
        self.poster = None
        self.overview = None
        self.genres = ["Science Fiction"]
        self.countries = ["India"]
        self.languages = ["en"]
        self.director = None
        self.runtime = None
        self.imdb_rating = 7.0
        self.popularity = 1.0
        self.released = "2001-01-01"


_orig_resolve = None
try:
    import app.metadata.providers as _prov

    _orig_resolve = _prov.resolve

    def _fake_resolve(title=None, year=None, slug=None, tmdb_key=None, **k):
        seen.append(title)
        return _FakeMeta(title)

    _prov.resolve = _fake_resolve
    _eng.__dict__["backfill_metadata"].__globals__["resolve"] = _fake_resolve

    out = _eng.backfill_metadata(t["id"], limit=50)
    check("backfill attempted the blank film", out["attempted"] >= 1, str(out))
    check("backfill enriched the blank film", out["enriched"] >= 1, str(out))
    check("it did not touch the two complete films",
          "Top Film" not in seen and "Runner Up" not in seen, str(seen))

    row = _db.one("SELECT genres, countries, meta FROM candidates"
                  " WHERE watchlist_id=? AND source_key=?", (t["id"], "bf0"))
    check("genres were written back", _db.loads(row["genres"], []) == ["Science Fiction"],
          row["genres"])
    check("countries were written back", _db.loads(row["countries"], []) == ["India"],
          row["countries"])
    check("unresolved flag cleared",
          _db.loads(row["meta"], {}).get("unresolved") is False, row["meta"])
finally:
    if _orig_resolve is not None:
        import app.metadata.providers as _prov

        _prov.resolve = _orig_resolve

print("\n5. A second backfill is a no-op once metadata is present")
seen.clear()
try:
    import app.metadata.providers as _prov

    def _fake_resolve2(title=None, year=None, slug=None, tmdb_key=None, **k):
        seen.append(title)
        return _FakeMeta(title)

    _eng.backfill_metadata.__globals__["resolve"] = _fake_resolve2
    out2 = _eng.backfill_metadata(t["id"], limit=50)
    check("nothing left to enrich", out2["enriched"] == 0, str(out2))
finally:
    if _orig_resolve is not None:
        import app.metadata.providers as _prov

        _prov.resolve = _orig_resolve

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)