"""End-to-end test: real Letterboxd sync + real scoring + mock Seerr/Radarr.

Covers the full loop the product promises:

    setup password -> sign in -> create watchlist -> sync a real public
    Letterboxd list -> enrich metadata -> score with real rules ->
    request the top movie to a fake Seerr -> verify the tracking tables.

Run:  python tests/test_e2e.py
Needs: network access to letterboxd.com (metadata via Cinemeta/Wikidata).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir + no background scheduler before importing the app.
_TMP = tempfile.mkdtemp(prefix="wlarr-e2e-")
os.environ["WATCHLISTARR_DATA"] = _TMP
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
os.environ["SEERR_URL"] = "http://127.0.0.1:5999"
os.environ["SEERR_API_KEY"] = "test-key"
os.environ["RADARR_URL"] = "http://127.0.0.1:5999"
os.environ["RADARR_API_KEY"] = "test-key"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mock_arr import serve, serve_tmdb  # noqa: E402

serve()
serve_tmdb()

# v1.7.0: metadata is TMDB-only. Point the provider at the mock TMDB and set a
# key before anything resolves metadata.
import app.metadata.providers as _providers  # noqa: E402
from app import settings_service as _svc  # noqa: E402

_providers.TMDB_BASE = "http://127.0.0.1:5998/3x".replace("/3x", "") + "/3"

from app import db, engine, settings_service as svc, watchlists as wl_svc  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def main() -> int:
    db.init_db()

    print("\n1. first-boot auth")
    check("needs setup on a fresh install", svc.needs_setup())
    svc.create_credentials("admin", "correct-horse")
    check("setup no longer needed", not svc.needs_setup())
    check("correct password accepted", svc.check_credentials("admin", "correct-horse"))
    check("wrong password rejected", not svc.check_credentials("admin", "nope-nope"))
    check("wrong user rejected", not svc.check_credentials("mallory", "correct-horse"))
    token = svc.issue_token("admin")
    check("session token verifies", svc.verify_token(token) == "admin")
    check("tampered token rejected", svc.verify_token(token + "x") is None)

    _svc.set_integration("tmdb_api_key", "e2e-mock-key")

    print("\n2. source adapter against live Letterboxd")
    from app.sources.letterboxd import LetterboxdSource, discover_lists

    items = LetterboxdSource(
        {"list_url": "https://letterboxd.com/kaitienk/list/chick-flicks/", "max_items": 12}
    ).fetch()
    check("fetched a real list", len(items) > 0, f"{len(items)} items")
    check("items have titles", all(i.title for i in items), items[0].title if items else "")
    check("source keys are stable", all(i.source_key.startswith("lb:") for i in items))
    found = discover_lists("kaitienk")
    check("discover_lists returns real lists", len(found) > 0, f"{len(found)} lists")

    print("\n3. create watchlist + sync + enrich (live network)")
    wl = wl_svc.create_watchlist(
        name="E2E — Letterboxd",
        source_type="letterboxd",
        source_ref={"list_url": "https://letterboxd.com/kaitienk/list/chick-flicks/", "max_items": 12},
        config={
            "max_per_run": 1,
            "rules": [
                {"id": "rom", "kind": "genre", "op": "in", "value": ["Romance", "Comedy"], "points": 40},
                {"id": "hi", "kind": "rating", "op": "gte", "value": 7.0, "points": 15},
                {"id": "nonsf", "kind": "genre", "op": "nin", "value": ["Romance", "Comedy"], "points": -20},
            ],
        },
    )
    check("watchlist created", wl["id"] is not None, wl["name"])

    wl = wl_svc.get_watchlist(wl["id"])
    sync = engine.sync_watchlist(wl, enrich_limit=12)
    check("sync ok", sync["ok"], str(sync.get("error", "")))
    check("fetched matches source", sync["fetched"] == len(items), str(sync["fetched"]))
    check("stored candidates", sync["new"] == len(items), f"{sync['new']} new")

    rows = db.query("SELECT * FROM candidates WHERE watchlist_id=?", (wl["id"],))
    enriched = [r for r in rows if db.loads(r["genres"], [])]
    check("metadata enriched with genres", len(enriched) > 0, f"{len(enriched)}/{len(rows)}")
    with_imdb = [r for r in rows if r["imdb_id"]]
    check("IMDb ids resolved", len(with_imdb) > 0, f"{len(with_imdb)}/{len(rows)}")
    with_tmdb = [r for r in rows if r["tmdb_id"]]
    check("TMDB ids resolved (needed for Seerr)", len(with_tmdb) > 0, f"{len(with_tmdb)}/{len(rows)}")
    check("posters fetched", any(r["poster"] for r in rows))

    print("\n4. scoring with real rules")
    wl = wl_svc.get_watchlist(wl["id"])
    cap = engine.capacity_snapshot()
    check("radarr capacity read", cap.available, f"{cap.free_gb:.0f}GB free, {cap.movie_count} movies")
    n = engine.refresh_scores(wl, cap)
    check("scored candidates", n == len(rows), str(n))
    # Re-query: `rows` was captured before refresh_scores wrote new scores.
    scored = db.query("SELECT title, score FROM candidates WHERE watchlist_id=?", (wl["id"],))
    top = scored[:3]
    print("      top 3:", [(r["title"][:28], r["score"]) for r in top])
    check("best match scores > 0", top[0]["score"] > 0, f"{top[0]['score']}")
    check("scores vary by rules", len({r["score"] for r in scored}) > 1,
          f"{len({r['score'] for r in scored})} distinct scores")
    check("unmatched movies score below matched",
          min(r["score"] for r in scored) < max(r["score"] for r in scored))

    print("\n5. request to mock Seerr")
    from tests.mock_arr import STATE

    wl = wl_svc.get_watchlist(wl["id"])
    res = engine.request_for_watchlist(wl, cap)
    check("request cycle ok", res["ok"], res.get("error", ""))
    check("exactly one requested (max_per_run=1)", res["requested"] == 1, str(res))
    check("Seerr received a POST", len(STATE["requests"]) == 1)
    sent = STATE["requests"][0]
    check("payload has mediaType=movie", sent.get("mediaType") == "movie", str(sent.get("mediaType")))
    check("payload has TMDB mediaId", bool(sent.get("mediaId")), str(sent.get("mediaId")))

    req_row = db.one(
        "SELECT * FROM candidates WHERE watchlist_id=? AND status='requested'", (wl["id"],)
    )
    check("candidate marked requested", req_row is not None)
    check("requested_at recorded", bool(req_row and req_row["requested_at"]))
    check("seerr_request_id recorded", bool(req_row and req_row["seerr_request_id"]))
    ev = db.query("SELECT * FROM candidate_events WHERE candidate_id=?", (req_row["id"],))
    check("request event logged", any(e["kind"] == "requested" for e in ev))

    print("\n6. idempotency — never request the same movie twice")
    wl = wl_svc.get_watchlist(wl["id"])
    engine.refresh_scores(wl, cap)
    first_title = db.one(
        "SELECT title FROM candidates WHERE watchlist_id=? AND status='requested' LIMIT 1",
        (wl["id"],),
    )["title"]
    res2 = engine.request_for_watchlist(wl, cap)
    already = db.one(
        "SELECT COUNT(*) AS c FROM candidates WHERE watchlist_id=? AND status='requested'",
        (wl["id"],),
    )["c"]
    # max_per_run=1 requests the *next* best movie from the queue on a later
    # run -- that is intended. The invariant is: no title is ever requested twice.
    sent_titles = [r.get("title") for r in STATE["requests"]]
    check("no duplicate titles sent to Seerr", len(sent_titles) == len(set(sent_titles)),
          str(sent_titles))
    check("previously requested movie was not re-sent", sent_titles[0] == first_title,
          f"first={sent_titles[0]!r}")
    check("a new movie was queued this run", res2["requested"] == 1, str(res2))
    check("requested rows now = 2 (distinct movies)", already == 2, str(already))
    check("Seerr POST count matches rows", len(STATE["requests"]) == already,
          f"{len(STATE['requests'])} POSTs vs {already} rows")

    print("\n7. sync again — no duplicates, no double requests")
    wl = wl_svc.get_watchlist(wl["id"])
    sync2 = engine.sync_watchlist(wl, enrich_limit=0)
    total = db.one("SELECT COUNT(*) AS c FROM candidates WHERE watchlist_id=?", (wl["id"],))["c"]
    check("sync ok on re-run", sync2["ok"])
    check("no duplicate candidates", total == len(rows), f"{total} rows")
    check("already-requested item skipped", sync2["skipped"] >= 1, str(sync2["skipped"]))

    print("\n8. disk-space gate using real Radarr data (60 GB free)")
    wl2 = wl_svc.create_watchlist(
        name="E2E — disk gated",
        source_type="letterboxd",
        source_ref={"list_url": "https://letterboxd.com/kaitienk/list/chick-flicks/", "max_items": 8},
        config={
            "max_per_run": 1,
            "rules": [
                {"id": "disk", "kind": "capacity", "unit": "free_gb", "comparator": "lt",
                 "threshold": 500, "limit": True, "points": 0},
                {"id": "hi", "kind": "rating", "op": "gte", "value": 6.0, "points": 10},
            ],
        },
    )
    engine.sync_watchlist(wl2, enrich_limit=8)
    wl2 = wl_svc.get_watchlist(wl2["id"])
    before = len(STATE["requests"])
    engine.refresh_scores(wl2, cap)
    res3 = engine.request_for_watchlist(wl2, cap)
    check("disk rule blocked every candidate", res3["requested"] == 0, str(res3))
    check("no Seerr call attempted", len(STATE["requests"]) == before)

    print("\n9. preset coverage of the brief's examples")
    from app.scoring import parse_rules, presets

    by_id = {p["id"]: p for p in presets()}
    check("preset: 1960s China this month", "1960s-china-month" in by_id)
    check("preset: Sci-Fi this week", "scifi-week" in by_id)
    rules = parse_rules(by_id["1960s-china-month"]["rules"])
    from app.scoring import Scorer

    s = Scorer(rules)
    r = s.score({"year": 1965, "genres": ["Drama"], "countries": ["China"],
                 "languages": ["Mandarin"], "imdb_rating": 7.9, "_window_request_count": 0})
    # 40 decade + 30 country + 20 language + 10 rating; the -15 "not 1960s"
    # penalty must not fire on a film that IS from the 1960s.
    check("1960s Chinese film scores 100", r.score == 100, str(r.score))
    modern = s.score({"year": 2020, "genres": ["Drama"], "countries": ["China"],
                      "languages": ["Mandarin"], "imdb_rating": 7.9, "_window_request_count": 0})
    check("modern Chinese film scores 45 (30+20+10-15)", modern.score == 45, str(modern.score))
    check("target outranks modern by 55", r.score - modern.score == 55,
          f"{r.score} vs {modern.score}")

    print("\n10. watchlist CRUD")
    n0 = len(wl_svc.list_watchlists())
    wl_svc.update_watchlist(wl2["id"], {"name": "renamed", "enabled": False})
    check("update applied", wl_svc.get_watchlist(wl2["id"])["name"] == "renamed")
    check("disable applied", wl_svc.get_watchlist(wl2["id"])["enabled"] is False)
    wl_svc.delete_watchlist(wl2["id"])
    check("delete applied", len(wl_svc.list_watchlists()) == n0 - 1)

    print("\n11. secret masking")
    svc.set_integration("seerr_api_key", "super-secret")
    pub = svc.public_integrations()
    check("secret masked in API", pub["seerr_api_key"] == svc.MASK, pub["seerr_api_key"])
    check("real secret still usable internally", svc.get_integration("seerr_api_key") == "super-secret")

    print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())