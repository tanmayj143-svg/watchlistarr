"""The engine: sync a watchlist -> enrich -> score -> request in Seerr.

One cycle per watchlist:

    1. fetch items from the source adapter
    2. upsert candidates (new metadata only for new/changed items)
    3. build a Radarr capacity snapshot (shared across watchlists in a run)
    4. score every pending candidate, respecting quota/capacity limits
    5. take the top N and POST them to Seerr, tracking what happened

Every step is idempotent: re-running never double-requests a movie, because a
candidate that reached `requested` keeps that status forever.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from . import db
from .arr.radarr import RadarrCapacity, RadarrClient
from .arr.seerr import SeerrClient
from .config import settings
from .metadata.providers import resolve as resolve_meta
from .scoring import Rule, Scorer, parse_rules
from .sources import SourceError, build as build_source
from .timewin import resolve_window

log = logging.getLogger("watchlistarr.engine")

# Concurrent metadata lookups. Sources are public and rate-limit tolerant; 8
# keeps a 25-item sync in seconds without hammering Cinemeta/Wikidata.
ENRICH_WORKERS = 8

STATUS_PENDING = "pending"
STATUS_REQUESTED = "requested"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"


def _integration(key: str, default: Any = None) -> Any:
    """DB-stored integration settings win over env defaults."""
    from .settings_service import get_integration

    return get_integration(key, default)


def _add_event(candidate_id: int, kind: str, detail: dict[str, Any] | None = None) -> None:
    db.insert(
        "INSERT INTO candidate_events(candidate_id, kind, detail, created_at) VALUES(?,?,?,?)",
        (candidate_id, kind, db.json.dumps(detail or {}), db.now()),
    )


# --------------------------------------------------------------------------
# 1. sync
# --------------------------------------------------------------------------
def sync_watchlist(wl: dict[str, Any], *, enrich_limit: int = 400) -> dict[str, Any]:
    """Fetch source items and upsert them as candidates."""
    run_id = db.insert(
        "INSERT INTO runs(watchlist_id, started_at) VALUES(?,?)", (wl["id"], db.now())
    )
    stats = {"fetched": 0, "new": 0, "updated": 0, "skipped": 0}
    try:
        adapter = build_source(wl["source_type"], wl["source_ref"])
        items = adapter.fetch()
        stats["fetched"] = len(items)
    except SourceError as exc:
        db.execute(
            "UPDATE runs SET finished_at=?, ok=0, error=?, stats=? WHERE id=?",
            (db.now(), str(exc), db.json.dumps(stats), run_id),
        )
        db.execute("UPDATE watchlists SET last_sync_at=? WHERE id=?", (db.now(), wl["id"]))
        log.warning("sync failed for %s: %s", wl.get("name"), exc)
        return {"ok": False, "error": str(exc), **stats}

    tmdb_key = _integration("tmdb_api_key", "") or None
    existing = {
        r["source_key"]: r
        for r in db.query(
            "SELECT * FROM candidates WHERE watchlist_id=?", (wl["id"],)
        )
    }

    new_items: list[Any] = []
    to_enrich = 0
    # Enrichment costs several network round-trips per movie, so resolve items
    # concurrently. Serial enrichment made a 25-item sync take minutes.
    #
    # BUG FIXED v1.3.9: this selected only items with NO existing row, so films
    # ingested while the budget was exhausted were stored with no genre,
    # country, language or ids -- 659 of 807 rows were effectively blank, which
    # is why genre/country rules matched almost nothing. Include existing rows
    # that were stored unresolved so a later sync can fill them in.
    pending_new = [
        i for i in items
        if i.source_key not in existing
        or db.loads(existing[i.source_key]["meta"], {}).get("unresolved")
    ]
    enrichment: dict[str, Any] = {}
    if pending_new and enrich_limit > 0:
        budget = pending_new[:enrich_limit]
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
            futures = {
                pool.submit(
                    resolve_meta,
                    title=it.title,
                    year=it.year,
                    imdb_id=it.imdb_id,
                    tmdb_id=it.tmdb_id,
                    slug=it.slug,
                    tmdb_key=tmdb_key,
                ): it.source_key
                for it in budget
            }
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    enrichment[key] = fut.result()
                except Exception as exc:  # one bad title must not kill a sync
                    log.warning("enrichment failed for %s: %s", key, exc)
                    enrichment[key] = None
        to_enrich = len(budget)

    for item in items:
        row = existing.get(item.source_key)
        # Already requested -> never touch it again.
        if row and row["status"] == STATUS_REQUESTED:
            stats["skipped"] += 1
            continue
        if row:
            # Re-write metadata for a previously unresolved film once we can.
            resolved = enrichment.get(item.source_key)
            if resolved is not None:
                db.execute(
                    "UPDATE candidates SET title=?, year=?, imdb_id=?, tmdb_id=?, poster=?,"
                    " overview=?, genres=?, countries=?, languages=?, director=?, runtime=?,"
                    " imdb_rating=?, popularity=?, released=?, meta=? WHERE id=?",
                    (
                        resolved.title or row["title"],
                        resolved.release_year or row["year"],
                        resolved.imdb_id, resolved.tmdb_id, resolved.poster,
                        resolved.overview,
                        db.json.dumps(resolved.genres),
                        db.json.dumps(resolved.countries),
                        db.json.dumps(resolved.languages),
                        resolved.director, resolved.runtime, resolved.imdb_rating,
                        resolved.popularity,
                        resolved.released or row["released"],
                        db.json.dumps({"unresolved": False}),
                        row["id"],
                    ),
                )
                stats["enriched"] = stats.get("enriched", 0) + 1

            # Backfill source_position for rows created before v1.3.2, where it
            # was stored in meta but never written to the column.
            pos = item.position
            if pos is None:
                pos = (db.loads(row["meta"], {}) or {}).get("position")
            pos = pos if pos is not None else 0
            if row["source_position"] is None:
                db.execute(
                    "UPDATE candidates SET title=?, year=?, slug=?, source_position=?, "
                    "updated_at=? WHERE id=?",
                    (item.title, item.year, item.slug, pos, db.now(), row["id"]),
                )
                stats["backfilled_positions"] = stats.get("backfilled_positions", 0) + 1
            else:
                db.execute(
                    "UPDATE candidates SET title=?, year=?, slug=?, source_position=?, "
                    "updated_at=? WHERE id=?",
                    (item.title, item.year, item.slug, pos, db.now(), row["id"]),
                )
            stats["updated"] += 1
            existing[item.source_key] = db.one(
                "SELECT * FROM candidates WHERE id=?", (row["id"],)
            )  # type: ignore[assignment]
            continue

        new_items.append(item)
        meta = enrichment.get(item.source_key)
        if meta is None:
            # Keep the row even without metadata so the UI can show it as
            # unresolved; it simply scores 0 and never gets requested.
            meta = None

        cid = db.insert(
            "INSERT INTO candidates(watchlist_id, source_key, title, year, slug, imdb_id, tmdb_id,"
            " poster, overview, genres, countries, languages, director, runtime, imdb_rating,"
            " released, popularity, source_position, meta, first_seen_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wl["id"], item.source_key,
                (meta.title if meta else item.title) or item.title,
                (meta.release_year if meta else item.year),
                item.slug,
                meta.imdb_id if meta else item.imdb_id,
                meta.tmdb_id if meta else item.tmdb_id,
                meta.poster if meta else item.poster,
                meta.overview if meta else None,
                db.json.dumps(meta.genres if meta else []),
                db.json.dumps(meta.countries if meta else []),
                db.json.dumps(meta.languages if meta else []),
                meta.director if meta else None,
                meta.runtime if meta else None,
                meta.imdb_rating if meta else None,
                # `meta` is None when enrichment failed (rate limit, unknown
                # film). Guard every attribute access -- this crashed syncs of
                # large lists where some titles cannot be resolved.
                (
                    (meta.released if meta else None)
                    or ((meta.year and f"{meta.year}-01-01") if meta else None)
                ),
                meta.popularity if meta else None,
                # Position in the source list. This is the tie-breaker that makes
                # the ranking deterministic; it used to stay NULL because the
                # value lived in item.meta and was never read back, so every film
                # tied and ordering fell back to insertion order.
                item.position if item.position is not None else 0,
                db.json.dumps({"unresolved": meta is None}),
                db.now(), db.now(),
            ),
        )
        _add_event(cid, "discovered", {"title": item.title, "resolved": meta is not None})
        stats["new"] += 1

    # Score immediately so the UI is never stale after a sync. The operator
    # asked for scoring to happen automatically rather than via a button.
    try:
        from .arr.radarr import RadarrCapacity as _RC

        _cap = capacity_snapshot()
        if not _cap.available:
            _cap = _RC(error="Radarr not configured")
        refresh_scores(wl, _cap)
        stats["scored"] = True
    except Exception as exc:  # scoring must never fail a sync
        log.warning("auto-score failed for %s: %s", wl.get("name"), exc)

    # Letterboxd ratings for newly discovered films, same reasoning: the
    # operator wants ratings available without pressing anything. Bounded so a
    # first sync of 800 films cannot spend forever on it.
    if new_items:
        try:
            got = backfill_letterboxd_ratings(
                wl["id"], limit=min(60, new_items), slugs=[i.slug for i in new_items if i.slug]
            )
            stats["lb_ratings"] = got.get("stored", 0)
        except Exception as exc:
            log.warning("auto letterboxd ratings failed for %s: %s", wl.get("name"), exc)
            stats["lb_ratings"] = 0

    db.execute(
        "UPDATE watchlists SET last_sync_at=?, last_sync_stats=? WHERE id=?",
        (db.now(), db.json.dumps(stats), wl["id"]),
    )
    db.execute(
        "UPDATE runs SET finished_at=?, ok=1, stats=? WHERE id=?",
        (db.now(), db.json.dumps(stats), run_id),
    )
    log.info("synced %s: %s", wl.get("name"), stats)
    return {"ok": True, **stats}


# --------------------------------------------------------------------------
# 2. scoring + requesting
# --------------------------------------------------------------------------
def _as_int(val: Any) -> int | None:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _truthy(val: Any) -> bool:
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)


def _global_storage_guards(cap: Any) -> dict[str, float]:
    """Read the two global storage thresholds from Settings.

    Values are in GB, blank/None means "disabled".
    """
    def _num(key: str) -> float | None:
        raw = _integration(key, None)
        if raw in (None, "", 0, "0"):
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    return {
        "min_free_gb": _num("min_free_gb"),
        "max_free_gb": _num("max_free_gb"),
        "est_movie_gb": _num("est_movie_gb"),
    }


def _apply_storage_guards(guards: dict[str, float], cap: Any) -> str | None:
    """Return a human-readable reason when a global guard blocks, else None."""
    if cap is None or not getattr(cap, "available", False):
        return None  # capacity unknown -> never block on storage
    free = getattr(cap, "free_gb", 0.0)

    min_free = guards.get("min_free_gb")
    if min_free is not None and free < min_free:
        return f"storage floor: only {free:.1f} GB free, needs {min_free:g} GB"

    max_free = guards.get("max_free_gb")
    if max_free is not None and free > max_free:
        return f"storage ceiling: {free:.1f} GB free is above the {max_free:g} GB ceiling, topping up paused"
    return None


def _window_counts(watchlist_id: int, now: datetime) -> dict[str, int]:
    """How many requests were already made in each named window."""
    out: dict[str, int] = {}
    rows = db.query(
        "SELECT requested_at FROM candidates WHERE watchlist_id=? AND requested_at IS NOT NULL",
        (watchlist_id,),
    )
    stamps = []
    for r in rows:
        try:
            stamps.append(datetime.fromisoformat(r["requested_at"]).astimezone(timezone.utc))
        except (TypeError, ValueError):
            continue
    for name in ("day", "week", "month", "quarter", "year"):
        w = resolve_window(name, now)
        out[name] = sum(1 for s in stamps if w.contains(s))
    return out


def capacity_snapshot() -> RadarrCapacity:
    url = _integration("radarr_url", settings.radarr_url)
    key = _integration("radarr_api_key", settings.radarr_api_key)
    if not url or not key:
        return RadarrCapacity(error="Radarr not configured")
    try:
        return RadarrClient(url, key, settings.http_timeout).capacity()
    except Exception as exc:  # never let Radarr break requesting
        log.warning("radarr capacity unavailable: %s", exc)
        return RadarrCapacity(error=str(exc))


def _candidate_payload(row: Any, counts: dict[str, int]) -> dict[str, Any]:
    d = db.row_to_candidate(row)
    d["_window_request_count"] = counts.get("month", 0)
    return d


def evaluate_watchlist(
    wl: dict[str, Any],
    cap: RadarrCapacity,
    *,
    manual: bool = False,
) -> list[dict[str, Any]]:
    """Score pending candidates. Returns them ranked, best first.

    `manual=True` disables the Pace intake valve so an operator clicking
    "Request Next" can always act on the top-ranked film. It does not change any
    score.
    """
    now = datetime.now(timezone.utc)
    cfg = wl.get("config", {}) or {}
    rules = parse_rules(cfg.get("rules", []))
    # The simple "X per Y" setting is expressed as a quota rule so both paths
    # share one enforcement mechanism (and one explanation in the UI).
    q_count = _as_int(cfg.get("quota_count"))
    if q_count and q_count > 0:
        rules = rules + [
            Rule(
                id="pace",
                kind="pace",
                window=(cfg.get("quota_window") or "month"),
                threshold=float(q_count),
                limit=True,
                points=0,
                enabled=True,
            )
        ]
    if manual:
        # An explicit human override outranks the Pace valve, whether the rule
        # came from the saved rules or was injected from quota_count above.
        rules = [r for r in rules if not r.limit or r.kind != "pace"]

    counts = _window_counts(wl["id"], now)
    scorer = Scorer(rules, capacity=cap)

    rows = db.query(
        "SELECT * FROM candidates WHERE watchlist_id=? AND status != ? ORDER BY score DESC, id ASC",
        (wl["id"], STATUS_REQUESTED),
    )
    # Global storage guards from Settings, applied to every watchlist.
    guards = _global_storage_guards(cap)
    ignore_radarr = _truthy(_integration("ignore_radarr_existing", True))
    # Digital-release filter (opt-in; default on).
    filter_undelivered = _truthy(_integration("filter_unreleased", True))
    tmdb_key = _integration("tmdb_api_key", "") or None
    if filter_undelivered:
        from .metadata import release as release_mod

        release_mod.warn_once_if_keyless(tmdb_key)

    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _candidate_payload(row, counts)
        # Each candidate sees the live count for every window it references.
        payload["_window_counts"] = counts

        # Gates decide ELIGIBILITY only. The score always comes from the rules,
        # so ranking stays meaningful while a film sits on hold. `gate` is the
        # first reason found, and must exist before any gate is tested.
        ignored = bool((payload.get("meta") or {}).get("ignored"))
        gate: dict[str, Any] | None = None
        if ignored:
            gate = {"rule": {"kind": "ignored"}, "reason": "skipped by you"}

        # Not yet digitally released -> hold back, but KEEP the row so the film
        # is reconsidered automatically on a later sync once it ships.
        if filter_undelivered and not manual:
            from .metadata import release as release_mod

            rs = release_mod.evaluate(
                tmdb_id=payload.get("tmdb_id"),
                released=payload.get("released"),
                year=payload.get("year"),
                tmdb_key=tmdb_key,
            )
            if not rs.available and gate is None:
                gate = {
                    "rule": {"kind": "not_released"},
                    "reason": f"not digitally released: {rs.note}",
                }

        # `ignore_radarr` is an automatic-intake guard. A manual "Request Next"
        # is an explicit operator decision, so it must not be vetoed by a film
        # Radarr already tracks -- the operator asked for it by name.
        if (
            ignore_radarr
            and not manual
            and cap is not None
            and getattr(cap, "available", False)
        ):
            known, why = cap.knows(
                tmdb_id=payload.get("tmdb_id"),
                imdb_id=payload.get("imdb_id"),
                title=payload.get("title"),
                year=payload.get("year"),
            )
            if known and gate is None:
                gate = {
                    "rule": {"kind": "radarr_exists"},
                    "reason": f"skipped: {why}",
                }

        res = scorer.score(payload, now=now)
        record = dict(payload)
        record["score"] = res.score
        record["matched"] = res.matched
        # A gate found above (ignored / not released / in Radarr) wins over the
        # scorer, but never over the score itself.
        record["eligible"] = res.eligible and gate is None
        record["rejected_by"] = gate or res.rejected_by

        # Digital-release verdict is attached in one place after scoring (see
        # below) so films short-circuited by an early `continue` also get one.


        # Global guards win over per-watchlist scoring -- except on a manual
        # request. The storage ceiling ("top up until free space is under X") is
        # an automatic-intake rule; clicking "Request Next" is a deliberate
        # instruction and must not be vetoed by it. The minimum-free-space guard
        # is the one exception: it protects against filling a genuinely full disk,
        # so it still applies.
        if res.eligible:
            effective = guards
            if manual:
                effective = {k: v for k, v in (guards or {}).items()
                             if k == "min_free_gb"}
            blocked = _apply_storage_guards(effective, cap)
            if blocked:
                record["eligible"] = False
                record["rejected_by"] = {"rule": {"kind": "storage_guard"}, "reason": blocked}
        out.append(record)
    # Deterministic ordering. Score is the ranking signal; the remaining keys are
    # tie-breakers so that films with equal points still come out in a sensible,
    # stable order instead of database insertion order.
    def _tie(r: dict[str, Any]) -> tuple:
        lb = r.get("lb_rating")
        imdb = r.get("imdb_rating")
        pop = r.get("popularity")
        # Ordering is by score ONLY -- higher first. Whether a film is currently
        # gated (pace, storage, already in Radarr) does not change its rank; it
        # only decides whether it can be requested right now. Eligibility is
        # reported separately as a status, so a full queue still ranks properly.
        return (
            -r["score"],
            # higher-rated first when scores tie
            -(lb if isinstance(lb, (int, float)) else -1),
            -(imdb if isinstance(imdb, (int, float)) else -1),
            -(pop if isinstance(pop, (int, float)) else -1),
            # then position in the source list (the user's own ordering)
            r.get("source_position") if r.get("source_position") is not None else 10**6,
            r.get("id") or 0,
        )

    out.sort(key=_tie)
    return out


def refresh_scores(wl: dict[str, Any], cap: RadarrCapacity | None = None) -> int:
    """Persist current scores so the UI can sort before any request happens."""
    cap = cap if cap is not None else capacity_snapshot()
    evaluated = evaluate_watchlist(wl, cap)

    # Digital-release verdict for every film, computed once here so films that
    # were short-circuited upstream (ignored / already in Radarr) still show a
    # Yes/No instead of "?".
    tmdb_key = _integration("tmdb_api_key", "") or None
    try:
        from .metadata import release as _rel

        # Parallel: doing this serially cost one network round-trip per film, so
        # re-scoring 807 films exceeded 400s and timed out.
        def _release_for(item: dict[str, Any]) -> dict[str, Any] | None:
            try:
                return _rel.evaluate(
                    tmdb_id=item.get("tmdb_id"),
                    released=item.get("released"),
                    year=item.get("year"),
                    tmdb_key=tmdb_key,
                    timeout=8,
                ).as_dict()
            except Exception:
                return None

        if evaluated:
            with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
                for item, verdict in zip(
                    evaluated, pool.map(_release_for, evaluated), strict=False
                ):
                    item["release"] = verdict
    except Exception as exc:
        log.warning("release check failed for %s: %s", wl.get("name"), exc)

    for item in evaluated:
        # Persist the verdict in meta so it survives across syncs.
        meta = dict(item.get("meta") or {})
        if item.get("release") is not None:
            meta["release"] = item["release"]
        db.execute(
            "UPDATE candidates SET score=?, breakdown=?, last_reason=?, meta=? WHERE id=?",
            (
                # Always persist the real score. Writing 0.0 for a gated film
                # destroyed the ranking: every film paused by Pace or a storage
                # guard was stored as 0 and looked identical to an unwanted film.
                item["score"],
                db.json.dumps(
                    {
                        "matched": item["matched"],
                        "eligible": item["eligible"],
                        "rejected_by": item["rejected_by"],
                    }
                ),
                (item["rejected_by"] or {}).get("reason") if item["rejected_by"] else None,
                db.json.dumps(meta),
                item["id"],
            ),
        )
    return len(evaluated)


def request_for_watchlist(
    wl: dict[str, Any],
    cap: RadarrCapacity | None = None,
    *,
    manual: bool = False,
) -> dict[str, Any]:
    """Send the top N eligible candidates to Seerr.

    `manual=True` is the "Request Next" button: the operator is overriding the
    intake valve by hand, so Pace and the per-run cap are bypassed. Scores are
    untouched either way -- ranking is always by score.
    """
    cap = cap if cap is not None else capacity_snapshot()
    stats = {"considered": 0, "requested": 0, "already": 0, "errors": 0, "rejected": 0}
    cfg = wl.get("config", {}) or {}

    # Service-level master switch.
    if not _integration("requests_enabled", True):
        return {**stats, "ok": False, "error": "Requests are disabled in Settings."}

    max_per_run = int(cfg.get("max_per_run") or 1)
    if manual:
        max_per_run = 1  # one film per click, but not capped by Pace
    elif max_per_run <= 0:
        return {**stats, "ok": True}

    root_folder = cfg.get("root_folder") or None
    quality_profile = cfg.get("quality_profile") or None
    tags = [t for t in (cfg.get("tags") or []) if t]
    search_on_add = cfg.get("search_on_add")

    seerr_url = _integration("seerr_url", settings.seerr_url)
    seerr_key = _integration("seerr_api_key", settings.seerr_api_key)
    if not seerr_url or not seerr_key:
        return {**stats, "ok": False, "error": "Seerr is not configured — add it in Settings."}

    try:
        client = SeerrClient(seerr_url, seerr_key, settings.http_timeout)
    except Exception as exc:
        return {**stats, "ok": False, "error": str(exc)}

    evaluated = evaluate_watchlist(wl, cap, manual=manual)
    sent = 0
    for item in evaluated:
        stats["considered"] += 1
        if not item["eligible"]:
            stats["rejected"] += 1
            continue
        if sent >= max_per_run:
            break
        # Never request something already in the library or already requested.
        if item["status"] == STATUS_REQUESTED:
            continue
        if not item.get("tmdb_id") and not item.get("imdb_id"):
            stats["errors"] += 1
            _add_event(item["id"], "skip", {"reason": "no tmdb/imdb id"})
            continue

        result = client.create_request(
            tmdb_id=item.get("tmdb_id"),
            title=item.get("title") or "",
            imdb_id=item.get("imdb_id"),
            root_folder=root_folder,
            quality_profile=quality_profile,
            tags=tags,
            search_on_add=search_on_add,
        )
        _add_event(
            item["id"],
            "requested" if result.ok else "error",
            {"detail": result.detail, "seerr_state": result.status, "score": item["score"]},
        )
        if result.ok:
            db.execute(
                "UPDATE candidates SET status=?, requested_at=?, seerr_request_id=?, "
                "seerr_state=?, updated_at=? WHERE id=?",
                (
                    STATUS_REQUESTED, db.now(), result.request_id,
                    result.status, db.now(), item["id"],
                ),
            )
            sent += 1
            stats["requested" if not result.already else "already"] += 1
        else:
            db.execute(
                "UPDATE candidates SET status=?, last_reason=?, updated_at=? WHERE id=?",
                (STATUS_ERROR, result.detail, db.now(), item["id"]),
            )
            stats["errors"] += 1

    log.info("request cycle for %s: %s", wl.get("name"), stats)
    return {**stats, "ok": True}


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def backfill_letterboxd_ratings(
    wl_id: int, limit: int = 60, slugs: list[str] | None = None
) -> dict[str, Any]:
    """Fetch and store Letterboxd community ratings for a watchlist.

    Optional and separate from sync because it needs one request per film; it is
    only run on demand (the "Fetch Letterboxd ratings" button) rather than on
    every cycle. Cached for a week per slug, so re-runs are cheap.
    """
    from .metadata.letterboxd_rating import fetch_many

    rows = db.query(
        "SELECT id, slug FROM candidates WHERE watchlist_id=? AND slug IS NOT NULL",
        (wl_id,),
    )
    if slugs:
        slugs = [s for s in slugs if s][:limit]
    else:
        slugs = [r["slug"] for r in rows][:limit]
    if not slugs:
        return {"ok": True, "fetched": 0, "stored": 0, "reason": "no slugs"}

    ratings = fetch_many(slugs)
    stored = 0
    for row in rows:
        hit = ratings.get(row["slug"])
        if not hit:
            continue
        value, votes = hit
        db.execute(
            "UPDATE candidates SET lb_rating=?, lb_votes=? WHERE id=?",
            (value, votes, row["id"]),
        )
        stored += 1
    log.info("letterboxd ratings for wl %s: %s stored of %s slugs", wl_id, stored, len(slugs))
    return {"ok": True, "fetched": len(slugs), "stored": stored}


def backfill_metadata(wl_id: int, limit: int = 400) -> dict[str, Any]:
    """Fill in missing metadata for stored candidates.

    Separate from sync because it needs one metadata lookup per film and can take
    several minutes on a large backlog. Safe to run repeatedly: it only touches
    rows flagged unresolved or missing genres.
    """
    rows = db.query(
        "SELECT id, title, year, slug, meta FROM candidates WHERE watchlist_id=? "
        "AND (genres IS NULL OR genres = '[]') "
        "ORDER BY (imdb_id IS NULL) + (tmdb_id IS NULL) ASC, id ASC LIMIT ?",
        (wl_id, max(1, min(limit, 800))),
    )
    if not rows:
        return {"ok": True, "attempted": 0, "enriched": 0, "remaining": 0}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .metadata.providers import resolve as _resolve

    tmdb_key = _integration("tmdb_api_key", "") or None
    # Wikidata is aggressively throttled for anonymous traffic; widen the pool
    # but let each call back off on its own rather than serialising everything.
    enriched = 0
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = {
            pool.submit(
                _resolve, title=r["title"], year=r["year"], slug=r["slug"], tmdb_key=tmdb_key
            ): r
            for r in rows
        }
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                m = fut.result()
            except Exception as exc:
                log.warning("backfill failed for %r: %s", r["title"], exc)
                continue
            if m is None:
                continue
            db.execute(
                "UPDATE candidates SET title=?, year=?, imdb_id=?, tmdb_id=?, poster=?,"
                " overview=?, genres=?, countries=?, languages=?, director=?, runtime=?,"
                " imdb_rating=?, popularity=?, released=?, meta=? WHERE id=?",
                (
                    m.title or r["title"], m.release_year or r["year"],
                    m.imdb_id, m.tmdb_id, m.poster, m.overview,
                    db.json.dumps(m.genres), db.json.dumps(m.countries),
                    db.json.dumps(m.languages), m.director, m.runtime,
                    m.imdb_rating, m.popularity, m.released,
                    db.json.dumps({"unresolved": False}),
                    r["id"],
                ),
            )
            enriched += 1
            if enriched % 25 == 0:
                log.info("metadata backfill wl %s: %s/%s filled so far",
                         wl_id, enriched, len(rows))

    remaining = db.one(
        "SELECT COUNT(*) AS c FROM candidates WHERE watchlist_id=? "
        "AND (genres IS NULL OR genres = '[]')",
        (wl_id,),
    )["c"]
    log.info("metadata backfill for wl %s: %s of %s filled, %s still empty",
             wl_id, enriched, len(rows), remaining)
    return {"ok": True, "attempted": len(rows), "enriched": enriched, "remaining": remaining}


def request_single_candidate(candidate_id: int) -> dict[str, Any]:
    """Request one specific film by id, ignoring all intake gates.

    Backs the per-row "Request" button. The operator picked this film
    deliberately, so Pace, the Radarr-exists guard and the digital-release gate
    are all bypassed. An explicitly skipped film stays skipped.
    """
    row = db.one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if row is None:
        return {"ok": False, "error": "No such candidate", "requested": 0}

    # sqlite3.Row has no .get(); wrap it before reading optional JSON columns.
    row = db.row_to_candidate(row)

    if (row.get("meta") or {}).get("ignored"):
        return {
            "ok": False, "requested": 0,
            "error": "This film is skipped. Un-skip it first if you want it.",
        }
    if row["status"] == STATUS_REQUESTED:
        return {"ok": False, "requested": 0, "error": "Already requested."}

    seerr_url = _integration("seerr_url", settings.seerr_url)
    seerr_key = _integration("seerr_api_key", settings.seerr_api_key)
    if not seerr_url or not seerr_key:
        return {"ok": False, "requested": 0, "error": "Seerr is not configured."}

    wl_row = db.one("SELECT config FROM watchlists WHERE id=?", (row["watchlist_id"],))
    wl_cfg = (db.loads(wl_row["config"], {}) if wl_row else {}) or {}
    wl_cfg_root = wl_cfg.get("root_folder") or None
    wl_cfg_quality = wl_cfg.get("quality_profile") or None
    wl_cfg_tags = [t for t in (wl_cfg.get("tags") or []) if t] or None

    payload = row
    try:
        client = SeerrClient(seerr_url, seerr_key, settings.http_timeout)
    except Exception as exc:
        return {"ok": False, "requested": 0, "error": str(exc)}

    try:
        # Seerr identifies a film by tmdb/imdb id plus title. Year is carried in
        # the title text for display only -- the API has no year field.
        result = client.create_request(
            tmdb_id=payload.get("tmdb_id"),
            title=payload.get("title") or "",
            imdb_id=payload.get("imdb_id"),
            root_folder=wl_cfg_root,
            quality_profile=wl_cfg_quality,
            tags=wl_cfg_tags,
            search_on_add=False,
        )
    except Exception as exc:
        db.execute("UPDATE candidates SET status='error', last_reason=? WHERE id=?",
                   (f"seerr error: {exc}", candidate_id))
        log.warning("manual request failed for %r: %s", payload.get("title"), exc)
        return {"ok": False, "requested": 0, "error": str(exc),
                "title": payload.get("title")}

    db.execute(
        "UPDATE candidates SET status=?, requested_at=?, seerr_request_id=?,"
        " seerr_state=?, score=?, last_reason=? WHERE id=?",
        (
            STATUS_REQUESTED,
            db.now(),
            getattr(result, "request_id", None),
            getattr(result, "status", None),
            payload.get("score") or 0.0,
            "requested manually from the watchlist",
            candidate_id,
        ),
    )
    # _add_event is per-candidate (candidate_id, kind, detail) -- it is not a
    # watchlist-level event log.
    _add_event(candidate_id, "manual_request",
               {"title": payload.get("title"),
                "seerr_request_id": getattr(result, "request_id", None)})
    log.info("manual request: %s -> seerr %s", payload.get("title"),
             getattr(result, "request_id", None))
    return {
        "ok": True, "requested": 1,
        "title": payload.get("title"),
        "seerr_request_id": getattr(result, "request_id", None),
        "already": bool(getattr(result, "already", False)),
    }


def run_cycle(*, sync: bool = True, request: bool = True) -> dict[str, Any]:
    """One full pass across all enabled watchlists."""
    lock = getattr(run_cycle, "_lock", None)
    if lock is None:
        lock = run_cycle._lock = threading.Lock()  # type: ignore[attr-defined]
    if not lock.acquire(blocking=False):
        return {"ok": False, "error": "A cycle is already running."}
    try:
        wls = [db.row_to_watchlist(r) for r in db.query(
            "SELECT * FROM watchlists WHERE enabled=1 ORDER BY sort_order, id"
        )]
        cap = capacity_snapshot()
        summary = {"watchlists": len(wls), "sync": [], "requests": [], "capacity": {
            "available": cap.available, "free_gb": round(cap.free_gb, 1),
            "movie_count": cap.movie_count, "error": cap.error,
        }}
        for wl in wls:
            if sync:
                s = sync_watchlist(wl)
                summary["sync"].append({"id": wl["id"], "name": wl["name"], **s})
                # Re-read config in case the sync wrote stats
                wl = db.row_to_watchlist(db.one("SELECT * FROM watchlists WHERE id=?", (wl["id"],)))  # type: ignore[arg-type]
            if request:
                refresh_scores(wl, cap)
                r = request_for_watchlist(wl, cap)
                summary["requests"].append({"id": wl["id"], "name": wl["name"], **r})
        summary["ok"] = True
        return summary
    finally:
        lock.release()