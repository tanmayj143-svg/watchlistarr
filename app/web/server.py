"""FastAPI application: pages, JSON API, auth, scheduler.

Mounts at :7777 by default. Session auth is a single signed cookie; the whole
UI is server-rendered plus one vanilla-JS frontend (no build step, no CDN), so
the container stays small and the UI works offline on a LAN.
"""
from __future__ import annotations

import hmac
import inspect
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette import status

from .. import db
from .. import explain as _explain, engine, security as sec, settings_service as svc, watchlists as wl_svc
from ..config import APP_VERSION, settings
from ..sources import SourceError
from ..sources.letterboxd import discover_lists
from ..scoring import COUNTRIES, GENRES, LANGUAGES

log = logging.getLogger("watchlistarr")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../app/web
APP_DIR = os.path.dirname(BASE_DIR)                            # .../app
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def render(request: Request, name: str, ctx: dict[str, Any], **kw: Any):
    """Render a template across Starlette's two TemplateResponse signatures.

    Starlette >= 0.29 requires `TemplateResponse(request, name, context)`;
    older releases took `TemplateResponse(name, context)`. Calling the old
    order against the new signature silently renders the dict as the template
    name (`TypeError: unhashable type: 'dict'`), so detect it once here.
    """
    params = inspect.signature(templates.TemplateResponse).parameters
    if "context" in params:                      # new signature
        return templates.TemplateResponse(request, name, ctx, **kw)
    return templates.TemplateResponse(name=name, context={**ctx}, **kw)  # type: ignore[arg-type]



# --------------------------------------------------------------------------
# auth dependency
# --------------------------------------------------------------------------
def current_user(
    # NB: FastAPI defaults the cookie name to the *parameter* name, so this
    # must be bound explicitly or every authenticated API call 401s.
    session: str | None = Cookie(default=None, alias=svc.SESSION_COOKIE),
) -> str | None:
    return svc.verify_token(session)


def require_user(user: str | None = Depends(current_user)) -> str:
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    db.init_db()
    log.info("Watchlistarr %s starting (data: %s)", APP_VERSION, settings.data_dir)
    _maybe_seed_watchlist()
    if settings.scheduler_enabled:
        _start_scheduler()
    yield
    _shutdown_scheduler.set()
    log.info("Watchlistarr stopped")


app = FastAPI(title="Watchlistarr", version=APP_VERSION, lifespan=lifespan)
_scheduler_thread: threading.Thread | None = None
_shutdown_scheduler = threading.Event()


# --------------------------------------------------------------------------
# scheduler
# --------------------------------------------------------------------------
def _interval(key: str, default_minutes: int) -> int:
    """Read a schedule interval from the database, falling back to env.

    Read on every reschedule so changing the frequency in Settings takes effect
    within a minute, without needing a container restart.
    """
    raw = svc.get_integration(key, None)
    try:
        minutes = int(raw) if raw not in (None, "") else default_minutes
    except (TypeError, ValueError):
        minutes = default_minutes
    return max(5, minutes)


def _should_retry_unmet_pace() -> bool:
    """Check if any enabled watchlist has an unmet frequency quota and pending candidates."""
    try:
        from datetime import datetime, timezone
        from .. import db
        from ..engine import _window_counts, STATUS_REQUESTED
        now = datetime.now(timezone.utc)
        watchlists = db.query("SELECT * FROM watchlists")
        for row in watchlists:
            wl = db.row_to_watchlist(row)
            cfg = wl.get("config") or {}
            if not cfg.get("enabled", True):
                continue
            q_count = cfg.get("quota_count")
            try:
                q_count = int(q_count) if q_count is not None else None
            except (ValueError, TypeError):
                q_count = None
            if not q_count or q_count <= 0:
                continue
            q_window = cfg.get("quota_window") or "month"
            counts = _window_counts(wl["id"], now)
            current_requested = counts.get(q_window, 0)
            if current_requested < q_count:
                pending = db.query(
                    "SELECT id FROM candidates WHERE watchlist_id=? AND status != ? LIMIT 1",
                    (wl["id"], STATUS_REQUESTED),
                )
                if pending:
                    return True
    except Exception as exc:
        log.warning("unmet frequency check failed: %s", exc)
    return False


def _scheduler_loop() -> None:
    import time
    from datetime import datetime, timedelta, timezone

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:  # scheduler is optional: manual Sync still works
        log.warning(
            "APScheduler is not installed — background sync is disabled. "
            "Use the Sync button, or `pip install APScheduler`."
        )
        return

    sched = BackgroundScheduler()

    def schedule_sync() -> None:
        mins = _interval("sync_frequency_minutes", settings.sync_interval_minutes)
        sched.add_job(
            engine.run_cycle,
            "interval",
            kwargs={"sync": True, "request": False},
            minutes=mins,
            id="sync",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        log.info("source re-check scheduled every %s min", mins)

    def schedule_request() -> None:
        mins = _interval("request_frequency_minutes", settings.request_interval_minutes)
        sched.add_job(
            engine.run_cycle,
            "interval",
            kwargs={"sync": False, "request": True},
            minutes=mins,
            id="request",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        log.info("request cycle scheduled every %s min", mins)

    schedule_sync()
    schedule_request()

    # Initial request pass shortly after boot (10s grace period)
    sched.add_job(
        engine.run_cycle,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=10),
        id="initial_request",
        kwargs={"sync": False, "request": True},
    )

    sched.start()

    # Re-apply the intervals periodically so a Settings change is picked up
    # within ~5 minutes, and check if an unmet frequency needs an automatic retry pass.
    last_unmet_retry = 0.0
    while not _shutdown_scheduler.wait(timeout=300):
        try:
            schedule_sync()
            schedule_request()
            now_ts = time.time()
            if now_ts - last_unmet_retry >= 900:  # 15 minutes minimum interval between retries
                if _should_retry_unmet_pace():
                    last_unmet_retry = now_ts
                    log.info("Automatic pace check: unmet frequency detected with pending candidates; running request cycle")
                    engine.run_cycle(sync=False, request=True)
        except Exception as exc:
            log.warning("scheduler reschedule failed: %s", exc)
    sched.shutdown(wait=False)


def _start_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _shutdown_scheduler.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()


def _maybe_seed_watchlist() -> None:
    """On a brand-new install, seed one example so the UI is not empty."""
    if db.one("SELECT id FROM watchlists LIMIT 1"):
        return
    wl_svc.create_watchlist(
        name="Example — Letterboxd",
        source_type="letterboxd",
        source_ref={"list_url": "https://letterboxd.com/dave/list/best-of-2025/"},
        config=wl_svc.DEFAULT_CONFIG,
        enabled=False,
    )
    log.info("seeded an example (disabled) watchlist")


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "version": APP_VERSION,
        "needs_setup": svc.needs_setup(),
        "user": svc.verify_token(request.cookies.get(svc.SESSION_COOKIE)),
        # Server-signed value; _with_csrf sets the matching cookie from it.
        "csrf_token": _issue_csrf(request),
        **extra,
    }



def _csrf_ok(request: Request, submitted: str) -> bool:
    """Validate a submitted CSRF field against the signed cookie."""
    cookie = request.cookies.get(sec.CSRF_COOKIE, "")
    if not cookie or not submitted:
        return False
    return hmac.compare_digest(submitted, cookie) and sec.valid_csrf(submitted)


def _issue_csrf(request: Request) -> str:
    """Return the page's CSRF token, minting one if the visitor has none.

    MUST run before rendering: _ctx() puts this value in the template, and the
    cookie is set from it afterwards. Reading the cookie during render (as an
    earlier revision did) produced an empty token on the first page view, so the
    form could never be submitted.
    """
    existing = request.cookies.get(sec.CSRF_COOKIE, "")
    if existing and sec.valid_csrf(existing):
        return existing
    return sec.mint_csrf()


def _with_csrf(response, request, token: str | None = None):
    """Attach the CSRF cookie if the visitor does not already have a valid one."""
    if token and not request.cookies.get(sec.CSRF_COOKIE):
        response.set_cookie(
            sec.CSRF_COOKIE,
            token,
            max_age=svc.SESSION_MAX_AGE,
            httponly=False,          # the page must be able to read it
            samesite="strict",
        )
    return response


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "app": "watchlistarr", "version": APP_VERSION}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if svc.needs_setup():
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    if not svc.verify_token(request.cookies.get(svc.SESSION_COOKIE)):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    wls = wl_svc.list_watchlists()
    stats = _home_stats(wls)
    ctx = _ctx(request, watchlists=wls, stats=stats, catalogue=wl_svc.catalogue())
    return _with_csrf(render(request, "home.html", ctx), request, ctx["csrf_token"])


def _home_stats(wls: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and top classifications for the dashboard."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds")
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")

    added_total = db.one(
        "SELECT COUNT(*) AS c FROM candidates WHERE status='requested'"
    )["c"]
    added_month = db.one(
        "SELECT COUNT(*) AS c FROM candidates WHERE status='requested' AND requested_at>=?",
        (month_ago,),
    )["c"]
    added_week = db.one(
        "SELECT COUNT(*) AS c FROM candidates WHERE status='requested' AND requested_at>=?",
        (week_ago,),
    )["c"]
    in_lists = db.one("SELECT COUNT(*) AS c FROM candidates")["c"]
    pending = db.one("SELECT COUNT(*) AS c FROM candidates WHERE status!='requested'")["c"]

    facets: dict[str, dict[str, int]] = {"genre": {}, "country": {}, "language": {}, "decade": {}}
    rows = db.query("SELECT genres, countries, languages, year, status FROM candidates")
    for r in rows:
        requested = r["status"] == "requested"
        for key, col in (("genre", "genres"), ("country", "countries"), ("language", "languages")):
            for val in db.loads(r[col], []):
                d = facets[key]
                d[val] = d.get(val, 0) + (1 if requested else 0)
        if r["year"] and requested:
            dec = (r["year"] // 10) * 10
            facets["decade"][dec] = facets["decade"].get(dec, 0) + 1

    top = {
        key: [
            {"name": name, "count": count}
            for name, count in sorted(vals.items(), key=lambda kv: -kv[1])[:12]
            if count > 0
        ]
        for key, vals in facets.items()
    }
    return {
        "added_total": added_total,
        "added_month": added_month,
        "added_week": added_week,
        "in_lists": in_lists,
        "pending": pending,
        "top_facets": top,
        "watchlist_count": len(wls),
    }


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    if svc.needs_setup():
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    ctx = _ctx(request, error=error)
    return _with_csrf(render(request, "login.html", ctx), request, ctx["csrf_token"])


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    if not _csrf_ok(request, csrf_token):
        return render(
            request,
            "login.html",
            _ctx(request, error="Your session expired. Please try again."),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    key = sec.client_key(request)
    allowed, retry = sec.check_login(username, False, key)
    if not allowed:
        log.warning("login throttled for %r from %s (%ss left)", username, key, retry)
        return render(
            request,
            "login.html",
            _ctx(request, error=f"Too many attempts. Try again in {retry // 60 + 1} minute(s)."),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not svc.check_credentials(username, password):
        # count the failure, then decide whether to lock
        allowed, retry = sec.check_login(username, False, key)
        msg = (
            f"Too many attempts. Try again in {retry // 60 + 1} minute(s)."
            if not allowed
            else "Wrong username or password."
        )
        return render(
            request,
            "login.html",
            _ctx(request, error=msg),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS if not allowed
            else status.HTTP_401_UNAUTHORIZED,
        )

    sec.check_login(username, True, key)
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        svc.SESSION_COOKIE,
        svc.issue_token(username),
        max_age=svc.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str | None = None):
    if not svc.needs_setup():
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    ctx = _ctx(request, error=error)
    return _with_csrf(render(request, "setup.html", ctx), request, ctx["csrf_token"])


@app.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    confirm: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    if not _csrf_ok(request, csrf_token):
        return render(
            request,
            "setup.html",
            _ctx(request, error="Your session expired. Please try again."),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not svc.needs_setup():
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if len(password) < 8:
        return render(
            request,
            "setup.html",
            _ctx(request, error="Password must be at least 8 characters."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if password != confirm:
        return render(
            request,
            "setup.html",
            _ctx(request, error="Passwords do not match."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    svc.create_credentials(username or "admin", password)
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        svc.SESSION_COOKIE,
        svc.issue_token(username or "admin"),
        max_age=svc.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(default="")):
    if not _csrf_ok(request, csrf_token):
        resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(sec.CSRF_COOKIE)
        return resp
    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(svc.SESSION_COOKIE)
    return resp


@app.get("/watchlist/{wl_id}", response_class=HTMLResponse)
def watchlist_page(request: Request, wl_id: int, user: str = Depends(require_user),
                   page: int = 1, per_page: int = 50,
                   sort: str = "score", direction: str = "desc"):
    wl = wl_svc.get_watchlist(wl_id)
    if wl is None:
        raise HTTPException(status_code=404, detail="No such watchlist")
    view = _watchlist_view(wl, page=page, per_page=per_page, sort=sort,
                           direction=direction)
    ctx = _ctx(request, wl=wl, **view, catalogue=wl_svc.catalogue())
    return _with_csrf(render(request, "watchlist.html", ctx), request, ctx["csrf_token"])


# Sort orders for the ranked pending table. SQL-level so an 800-row watchlist
# paginates instead of rendering every row and every poster.
SORT_FIELDS = {
    "score": "score",
    "title": "title",
    "year": "year",
    "rating": "lb_rating",
    "added": "first_seen_at",
    "position": "source_position",
}


def _decorate(r: Any) -> dict[str, Any]:
    """Attach the fields the watchlist template needs to one candidate row."""
    c = db.row_to_candidate(r)
    # Digital-release verdict is stored at scoring time, so the UI always has
    # Yes/No without re-querying TMDB on every page load.
    c["release"] = (c.get("meta") or {}).get("release")
    blocked = ((c.get("breakdown") or {}).get("rejected_by") or {})
    kind = (blocked.get("rule") or {}).get("kind")
    c["blocked_by"] = kind if kind else None
    c["blocked_reason"] = blocked.get("reason")
    # Why = scoring only. Status = intake gate. The two are never conflated.
    c["why_lines"] = _explain.explain(c)
    c["why_summary"] = _explain.summary(c)
    c["status_line"] = _explain.status_line(c)
    return c


def _watchlist_view(
    wl: dict[str, Any],
    *,
    page: int = 1,
    per_page: int = 50,
    sort: str = "score",
    direction: str = "desc",
) -> dict[str, Any]:
    """Watchlist data.

    `pending` is paginated and sorted in SQL. The full list is still counted so
    the UI can show "showing 1-50 of 802" and render real page links.
    """
    page = max(1, int(page or 1))
    per_page = max(10, min(int(per_page or 50), 200))
    col = SORT_FIELDS.get(sort, "score")
    direction = "asc" if str(direction).lower() == "asc" else "desc"

    # Nulls always sort last regardless of direction, so films with no rating do
    # not crowd the top of an ascending sort.
    order = (f"ORDER BY ({col} IS NULL OR {col} = '') ASC, {col} {direction.upper()}, "
             f"id {direction.upper()}")

    ignored_clause = "AND COALESCE(json_extract(meta, '$.ignored'), 0) = 0"
    total = db.one(
        f"SELECT COUNT(*) AS c FROM candidates WHERE watchlist_id=? "
        f"AND status != 'requested' {ignored_clause}",
        (wl["id"],),
    )["c"]
    pages = max(1, (total + per_page - 1) // per_page)
    # Clamp before computing the offset: clamping after left the last page
    # fetching from a stale offset, so page 5 of 5 returned 20 rows, not 25.
    page = min(max(1, page), pages)
    offset = (page - 1) * per_page

    pending_rows = db.query(
        f"SELECT * FROM candidates WHERE watchlist_id=? AND status != 'requested' "
        f"{ignored_clause} {order} LIMIT ? OFFSET ?",
        (wl["id"], per_page, offset),
    )
    pending = [_decorate(r) for r in pending_rows]

    # Requested + skipped are small enough to return whole.
    added_rows = db.query(
        "SELECT * FROM candidates WHERE watchlist_id=? AND status='requested' "
        "ORDER BY requested_at DESC, id DESC LIMIT 200",
        (wl["id"],),
    )
    added = [_decorate(r) for r in added_rows]

    skipped_rows = db.query(
        "SELECT * FROM candidates WHERE watchlist_id=? AND status != 'requested' "
        "AND COALESCE(json_extract(meta, '$.ignored'), 0) = 1 "
        "ORDER BY score DESC, id DESC LIMIT 200",
        (wl["id"],),
    )
    skipped = [_decorate(r) for r in skipped_rows]

    counts = engine._window_counts(wl["id"], None)  # type: ignore[arg-type]
    cap = engine.capacity_snapshot()
    return {
        "candidates": pending,
        "added": added,
        "pending": pending,
        "skipped": skipped,
        "pg": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "direction": direction,
            "from": offset + 1 if total else 0,
            "to": min(offset + per_page, total),
        },
        "window_counts": counts,
        "capacity": {
            "available": cap.available,
            "free_gb": round(cap.free_gb, 1),
            "total_gb": round(cap.total_gb, 1),
            "movie_count": cap.movie_count,
            "error": cap.error,
        },
        "recent_runs": [
            {**dict(r), "stats": db.loads(r["stats"], {})} for r in db.query(
                "SELECT * FROM runs WHERE watchlist_id=? ORDER BY id DESC LIMIT 10", (wl["id"],)
            )
        ],
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(require_user)):
    ctx = _ctx(
        request,
        integrations=svc.public_integrations(),
        saved=request.query_params.get("saved"),
    )
    return _with_csrf(render(request, "settings.html", ctx), request, ctx["csrf_token"])


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
def _err(message: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=code)


@app.get("/api/catalogue")
def api_catalogue(user: str = Depends(require_user)):
    return {
        **wl_svc.catalogue(),
        "genres": GENRES,
        "countries": COUNTRIES,
        "languages": LANGUAGES,
    }


@app.get("/api/watchlists")
def api_watchlists(user: str = Depends(require_user)):
    return wl_svc.list_watchlists()


@app.post("/api/watchlists")
async def api_watchlist_create(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    try:
        wl = wl_svc.create_watchlist(
            name=body.get("name") or "New watchlist",
            source_type=body.get("source_type") or "letterboxd",
            source_ref=body.get("source_ref") or {},
            config=body.get("config"),
            accent=body.get("accent"),
            enabled=bool(body.get("enabled", True)),
        )
    except ValueError as exc:
        return _err(str(exc))
    return {"ok": True, "watchlist": wl}


@app.patch("/api/watchlists/{wl_id}")
async def api_watchlist_update(wl_id: int, request: Request, user: str = Depends(require_user)):
    body = await request.json()
    wl = wl_svc.update_watchlist(wl_id, body)
    if wl is None:
        return _err("No such watchlist", 404)
    return {"ok": True, "watchlist": wl}


@app.delete("/api/watchlists/{wl_id}")
def api_watchlist_delete(wl_id: int, user: str = Depends(require_user)):
    if not wl_svc.delete_watchlist(wl_id):
        return _err("No such watchlist", 404)
    return {"ok": True}


@app.post("/api/watchlists/{wl_id}/letterboxd-ratings")
def api_lb_ratings(
    wl_id: int,
    request: Request,
    user: str = Depends(require_user),
):
    """Fetch Letterboxd ratings for films that do not have one yet.

    Normally unnecessary -- sync() populates ratings for new films automatically
    -- but kept as an escape hatch for a backlog (and for the first run on a
    large pre-existing watchlist). Cached slugs are skipped, so repeated calls are
    cheap and safe to loop.
    """
    try:
        limit = int(request.query_params.get("limit", 250))
    except (TypeError, ValueError):
        limit = 250
    limit = max(1, min(limit, 800))
    return engine.backfill_letterboxd_ratings(wl_id, limit=limit)


@app.get("/api/watchlists/{wl_id}")
def api_watchlist(wl_id: int, user: str = Depends(require_user),
                  page: int = 1, per_page: int = 50,
                  sort: str = "score", direction: str = "desc"):
    wl = wl_svc.get_watchlist(wl_id)
    if wl is None:
        return _err("No such watchlist", 404)
    view = _watchlist_view(wl, page=page, per_page=per_page, sort=sort,
                           direction=direction)
    return {"watchlist": wl, **{k: v for k, v in view.items() if k != "recent_runs"}}


@app.post("/api/watchlists/{wl_id}/sync")
def api_sync(wl_id: int, user: str = Depends(require_user)):
    wl = wl_svc.get_watchlist(wl_id)
    if wl is None:
        return _err("No such watchlist", 404)
    return engine.sync_watchlist(wl)


@app.post("/api/watchlists/{wl_id}/score")
def api_score(wl_id: int, user: str = Depends(require_user)):
    wl = wl_svc.get_watchlist(wl_id)
    if wl is None:
        return _err("No such watchlist", 404)
    n = engine.refresh_scores(wl)
    return {"ok": True, "scored": n}


@app.post("/api/watchlists/{wl_id}/request")
def api_request(wl_id: int, user: str = Depends(require_user)):
    """Scheduled/automatic request -- respects the frequency limit."""
    wl = wl_svc.get_watchlist(wl_id)
    if wl is None:
        return _err("No such watchlist", 404)
    engine.refresh_scores(wl)
    return engine.request_for_watchlist(wl)


@app.post("/api/watchlists/{wl_id}/request-next")
def api_request_next(wl_id: int, user: str = Depends(require_user)):
    """The "Request Next" button: a manual override.

    The operator is deliberately overriding the intake valve, so Pace is
    bypassed and the top-ranked film is requested even when the weekly cap is
    spent. Scores are unaffected -- this only changes eligibility for this one
    action.
    """
    wl = wl_svc.get_watchlist(wl_id)
    if wl is None:
        return _err("No such watchlist", 404)
    engine.refresh_scores(wl)
    res = engine.request_for_watchlist(wl, manual=True)
    if res.get("ok") and not res.get("requested"):
        res["note"] = "Nothing to request -- the top films are already in Radarr or Seerr."
    return res


@app.post("/api/candidates/{candidate_id}/request")
def api_request_candidate(candidate_id: int, user: str = Depends(require_user)):
    """Request one specific film -- backs the per-row Request button.

    Bypasses the frequency limit, the Radarr-exists guard and the digital-release gate, because
    the operator picked this film deliberately. A skipped film stays skipped.
    """
    return engine.request_single_candidate(candidate_id)


@app.post("/api/watchlists/{wl_id}/backfill")
def api_backfill(wl_id: int, user: str = Depends(require_user)):
    """Fill in missing genre/country/language metadata for stored films."""
    wl = wl_svc.get_watchlist(wl_id)
    if wl is None:
        return _err("No such watchlist", 404)
    return engine.backfill_metadata(wl_id)


@app.post("/api/cycle")
def api_cycle(user: str = Depends(require_user)):
    return engine.run_cycle()


@app.get("/api/candidates/{candidate_id}")
def api_candidate(candidate_id: int, user: str = Depends(require_user)):
    row = db.one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if row is None:
        return _err("No such movie", 404)
    cand = db.row_to_candidate(row)
    events = [
        {**dict(e), "detail": db.loads(e["detail"], {})}
        for e in db.query(
            "SELECT * FROM candidate_events WHERE candidate_id=? ORDER BY id DESC", (candidate_id,)
        )
    ]
    return {"candidate": cand, "events": events}


@app.post("/api/candidates/{candidate_id}/ignore")
async def api_ignore(candidate_id: int, request: Request, user: str = Depends(require_user)):
    body = await request.json()
    if not db.one("SELECT id FROM candidates WHERE id=?", (candidate_id,)):
        return _err("No such movie", 404)
    db.execute(
        "UPDATE candidates SET meta=?, last_reason=?, updated_at=? WHERE id=?",
        (
            db.json.dumps({**(db.loads(db.one("SELECT meta FROM candidates WHERE id=?", (candidate_id,))["meta"], {})), "ignored": True,
                           "ignore_reason": body.get("reason") or ""}),
            "ignored by user",
            db.now(),
            candidate_id,
        ),
    )
    engine._add_event(candidate_id, "ignored", {"reason": body.get("reason")})
    return {"ok": True}


@app.post("/api/candidates/{candidate_id}/unignore")
def api_unignore(candidate_id: int, user: str = Depends(require_user)):
    row = db.one("SELECT meta FROM candidates WHERE id=?", (candidate_id,))
    if row is None:
        return _err("No such movie", 404)
    meta = db.loads(row["meta"], {})
    meta["ignored"] = False
    db.execute(
        "UPDATE candidates SET meta=?, last_reason=NULL, updated_at=? WHERE id=?",
        (db.json.dumps(meta), db.now(), candidate_id),
    )
    engine._add_event(candidate_id, "unignored")
    return {"ok": True}


@app.get("/api/discover/letterboxd")
def api_discover(username: str, user: str = Depends(require_user)):
    try:
        return {"ok": True, "lists": discover_lists(username)}
    except SourceError as exc:
        return _err(str(exc))


@app.post("/api/settings/integrations")
async def api_settings_save(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    saved = []
    for key, val in (body or {}).items():
        if key not in svc.INTEGRATION_KEYS:
            continue
        if val == svc.MASK:  # untouched masked secret
            continue
        svc.set_integration(key, val)
        saved.append(key)
    return {"ok": True, "saved": saved}


@app.post("/api/settings/password")
async def api_password(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    current = body.get("current") or ""
    new = body.get("new") or ""
    if not svc.check_credentials(db.get_setting("auth.username"), current):
        return _err("Current password is wrong.", 403)
    if len(new) < 8:
        return _err("New password must be at least 8 characters.")
    svc.set_password(new)
    return {"ok": True}


@app.post("/api/settings/test/{service}")
def api_test_service(service: str, user: str = Depends(require_user)):
    from ..arr.radarr import RadarrClient
    from ..arr.seerr import SeerrClient

    if service == "seerr":
        try:
            ok, msg = SeerrClient(
                svc.get_integration("seerr_url", settings.seerr_url),
                svc.get_integration("seerr_api_key", settings.seerr_api_key),
            ).test()
        except Exception as exc:
            return _err(str(exc))
    elif service == "radarr":
        try:
            ok, msg = RadarrClient(
                svc.get_integration("radarr_url", settings.radarr_url),
                svc.get_integration("radarr_api_key", settings.radarr_api_key),
            ).test()
        except Exception as exc:
            return _err(str(exc))
    else:
        return _err(f"Unknown service '{service}'")
    return {"ok": ok, "message": msg}


@app.get("/api/stats/facets")
def api_facets(user: str = Depends(require_user)):
    wls = wl_svc.list_watchlists()
    return _home_stats(wls)


class _VersionedStatic(StaticFiles):
    """Static files with an explicit no-cache policy.

    Without Cache-Control, browsers heuristically cache /static/app.js and keep
    serving stale JavaScript after a deploy -- which repeatedly looked like
    "my fix did not appear". Content is also addressable by ?v=APP_VERSION.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp


app.mount(
    "/static",
    _VersionedStatic(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)