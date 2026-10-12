"""Integration settings + password auth.

Auth model (per the brief): a single admin login/password created by a prompt on
first boot. The password is stored as an Argon2-style PBKDF2-SHA256 hash with a
per-user salt; the plaintext is never written to disk. Sessions are signed
cookies, so no session store is needed.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from typing import Any

from . import db

log = logging.getLogger("watchlistarr.settings")

PBKDF2_ROUNDS = 260_000
SESSION_COOKIE = "wl_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

# Keys the UI may read/write through /api/settings.
INTEGRATION_KEYS = {
    "seerr_url",
    "seerr_api_key",
    "radarr_url",
    "radarr_api_key",
    "tmdb_api_key",
    "requests_enabled",
    "metadata_provider",
    "http_proxy",
    "letterboxd_username_default",
    # --- global storage guards (v1.1.0) -------------------------------
    # Only request while free space is >= min_free_gb.
    "min_free_gb",
    # Keep requesting only while free space is <= max_free_gb, so the drive is
    # never left more than this much empty.
    "max_free_gb",
    # Estimate the size of one request, used to decide whether a request would
    # breach max_free_gb before making it. 0 disables the lookahead.
    "est_movie_gb",
    # Skip films Radarr already knows (including deleted ones).
    "ignore_radarr_existing",
    # Do not request films that have not had a digital release yet.
    "filter_unreleased",
    # How often the scheduler re-checks sources for newly added films.
    "sync_frequency_minutes",
    # How often a request cycle runs.
    "request_frequency_minutes",
    # Skip anything Watchlistarr has requested before, even after the user
    # cleared its local history.
    "ignore_previously_requested",
}
# Keys that are secrets: returned masked, stored as-is.
SECRET_KEYS = {"seerr_api_key", "radarr_api_key", "tmdb_api_key", "http_proxy"}

MASK = "••••••••"


def get_integration(key: str, default: Any = None) -> Any:
    """DB value first, env var as fallback (so docker env still works)."""
    val = db.get_setting(f"integration.{key}")
    if val not in (None, ""):
        return val
    env_val = os.getenv(key.upper())
    if env_val:
        return env_val
    return default


def set_integration(key: str, value: Any) -> None:
    db.set_setting(f"integration.{key}", value)


def public_integrations() -> dict[str, Any]:
    """Integrations with secrets masked, ready for the settings page."""
    from .config import settings

    out: dict[str, Any] = {}
    for key in INTEGRATION_KEYS:
        val = get_integration(key)
        if val in (None, ""):
            # fall back to the config defaults so the page can prefill
            val = {
                "seerr_url": settings.seerr_url,
                "seerr_api_key": settings.seerr_api_key,
                "radarr_url": settings.radarr_url,
                "radarr_api_key": settings.radarr_api_key,
            }.get(key)
        out[key] = MASK if (val and key in SECRET_KEYS) else val
    return out


# --------------------------------------------------------------------------
# password
# --------------------------------------------------------------------------
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, AttributeError):
        return False


def needs_setup() -> bool:
    return not db.get_setting("auth.password")


def create_credentials(username: str, password: str) -> None:
    db.set_setting("auth.username", username.strip() or "admin")
    db.set_setting("auth.password", hash_password(password))
    log.info("created admin credentials for user %r", username)


def set_password(password: str) -> None:
    db.set_setting("auth.password", hash_password(password))


def check_credentials(username: str, password: str) -> bool:
    stored_user = db.get_setting("auth.username") or "admin"
    stored_hash = db.get_setting("auth.password") or ""
    if not stored_hash:
        return False
    # Constant-ish work regardless of username match.
    ok_user = hmac.compare_digest(str(username or ""), str(stored_user))
    ok_pass = verify_password(password or "", stored_hash)
    return ok_user and ok_pass


# --------------------------------------------------------------------------
# sessions (HMAC-signed cookie, no server-side store)
# --------------------------------------------------------------------------
def _secret() -> bytes:
    secret = db.get_setting("auth.session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        db.set_setting("auth.session_secret", secret)
    return str(secret).encode()


def issue_token(username: str) -> str:
    payload = f"{username}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, sig):
        return payload
    return None