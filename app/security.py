"""Login throttling and CSRF protection.

Two gaps found in the pre-production security review (v1.1.4):

1. **No rate limit on the login form.** PBKDF2 makes guessing expensive, but an
   unbounded number of attempts is still a denial-of-service and CPU-burn
   vector, and lets an attacker keep trying forever.
2. **No CSRF token on form posts.** The session cookie is SameSite=Lax, which
   blocks cross-site POSTs from forms in modern browsers, but SameSite is a
   browser-level mitigation rather than a guarantee and is ignored by older
   agents. A double-submit token closes it properly.

Both are deliberately dependency-free: a fixed-size in-memory table for the
throttle (single-process app) and an HMAC-derived token for CSRF.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from collections import defaultdict

from .settings_service import _secret

log = logging.getLogger("watchlistarr.security")

# ---------------------------------------------------------------- throttling

MAX_ATTEMPTS = 8          # per window
WINDOW_SECONDS = 300      # 5 minutes
BLOCK_SECONDS = 900       # 15 minutes once tripped

_attempts: dict[str, list[float]] = defaultdict(list)
_blocked_until: dict[str, float] = {}
_lock = threading.Lock()


def client_key(request) -> str:
    """Best-effort client identity.

    Only used for throttling, so a spoofed X-Forwarded-For merely gives an
    attacker a fresh bucket -- it cannot be used to bypass the limit on a single
    source IP. uvicorn is started with proxy_headers=True, so this reflects the
    real client when deployed behind a reverse proxy.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_login(username: str, ok: bool, key: str) -> tuple[bool, int]:
    """Record a login attempt. Returns (allowed, retry_after_seconds)."""
    ident = f"{key}:{(username or '').lower()}"
    now = time.time()

    with _lock:
        until = _blocked_until.get(ident)
        if until and until > now:
            return False, int(until - now)
        if until and until <= now:
            _blocked_until.pop(ident, None)
            _attempts.pop(ident, None)

        # Prune old successes/failures outside the window.
        _attempts[ident] = [t for t in _attempts[ident] if now - t < WINDOW_SECONDS]

        if ok:
            _attempts.pop(ident, None)
            return True, 0

        _attempts[ident].append(now)
        if len(_attempts[ident]) >= MAX_ATTEMPTS:
            _blocked_until[ident] = now + BLOCK_SECONDS
            _attempts.pop(ident, None)
            log.warning("login throttle tripped for %s (from %s)", username, key)
            return False, BLOCK_SECONDS
        return True, 0


def reset_login(username: str, key: str) -> None:
    with _lock:
        _attempts.pop(f"{key}:{(username or '').lower()}", None)
        _blocked_until.pop(f"{key}:{(username or '').lower()}", None)


# ---------------------------------------------------------------------- CSRF

CSRF_COOKIE = "wl_csrf"
CSRF_FIELD = "csrf_token"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _sign(token: str) -> str:
    return hmac.new(_secret(), token.encode(), "sha256").hexdigest()[:32]


def valid_csrf(token: str | None) -> bool:
    """Double-submit check: the cookie must be signed by the server."""
    if not token or "." not in token:
        return False
    value, _, sig = token.rpartition(".")
    return hmac.compare_digest(_sign(value), sig)


def mint_csrf() -> str:
    raw = new_csrf_token()
    return f"{raw}.{_sign(raw)}"


def check_csrf_from(request) -> bool:
    """Validate the submitted field against the signed cookie."""
    field = request.headers.get("x-csrf-token")
    if not field and hasattr(request, "_form"):
        field = request._form.get(CSRF_FIELD)
    if not field:
        return False
    cookie = request.cookies.get(CSRF_COOKIE, "")
    return bool(cookie) and hmac.compare_digest(field, cookie) and valid_csrf(field)