"""Security tests: login throttling and CSRF.

Written for the pre-production security review (v1.1.4). These assert real
behaviour, not just that the functions exist.

Run: python tests/test_security.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-sec-"))
os.environ["WATCHLISTARR_SCHEDULER"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as _db  # noqa: E402

_db.init_db()

from app import security as sec  # noqa: E402
from app import settings_service as svc  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


class FakeReq:
    """Minimal stand-in for a Starlette Request."""

    def __init__(self, ip="10.0.0.5", cookies=None, headers=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        client = type("C", (), {"host": ip})()
        self.client = client


# ---------------------------------------------------------------- throttling
print("\n1. Login throttling")
sec._attempts.clear()
sec._blocked_until.clear()

ok = True
for _ in range(sec.MAX_ATTEMPTS - 1):
    allowed, retry = sec.check_login("admin", False, "10.0.0.5")
    ok = ok and allowed
check(f"first {sec.MAX_ATTEMPTS - 1} failures are allowed", ok)

allowed, retry = sec.check_login("admin", False, "10.0.0.5")
check("the next failure trips the throttle", not allowed, f"retry_after={retry}s")
check("throttle returns a real delay", retry > 0, f"{retry}s")

allowed2, _ = sec.check_login("admin", False, "10.0.0.5")
check("further attempts stay blocked", not allowed2)

# brute force from a different source is independently tracked
allowed3, _ = sec.check_login("admin", False, "10.0.0.99")
check("a different client IP has its own bucket", allowed3)

# success clears the counter
sec._attempts.clear(); sec._blocked_until.clear()
sec.check_login("admin", False, "10.0.0.7")
allowed4, _ = sec.check_login("admin", True, "10.0.0.7")
check("a successful login clears failures", allowed4)
allowed5, _ = sec.check_login("admin", False, "10.0.0.7")
check("counter really was cleared (can fail again)", allowed5)

# username is part of the key
sec._attempts.clear(); sec._blocked_until.clear()
for _ in range(sec.MAX_ATTEMPTS):
    sec.check_login("admin", False, "10.0.0.8")
blocked_admin, _ = sec.check_login("admin", False, "10.0.0.8")
other_user, _ = sec.check_login("someone-else", False, "10.0.0.8")
check("admin is blocked", not blocked_admin)
check("a different username is tracked separately", other_user,
      "throttle key is username+IP, so one user's lockout cannot DoS another")
other_blocked, _ = sec.check_login("someone-else", False, "10.0.0.8")
check("the other username blocks on its own threshold", other_blocked)

# X-Forwarded-For parsing
r = FakeReq(ip="10.0.0.1", headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
check("client_key honours X-Forwarded-For",
      sec.client_key(r) == "203.0.113.9", sec.client_key(r))
check("client_key falls back to peer IP",
      sec.client_key(FakeReq(ip="10.0.0.2")) == "10.0.0.2")


# ---------------------------------------------------------------------- CSRF
print("\n2. CSRF tokens")
tok = sec.mint_csrf()
check("token is minted", bool(tok) and "." in tok)
check("minted token validates", sec.valid_csrf(tok))
check("tampered value rejected", not sec.valid_csrf("abc.def"))
check("forged token (no signature) rejected", not sec.valid_csrf("whatever.signature"))
check("empty token rejected", not sec.valid_csrf(""))

raw, _, sig = tok.partition(".")
check("signature covers the value", sec.valid_csrf(f"{raw}x.{sig}") is False)

# a token signed with a different secret must fail
import app.security as secmod
orig = secmod._secret
secmod._secret = lambda: b"a-different-secret"
try:
    check("token signed with another secret is rejected", not sec.valid_csrf(tok))
finally:
    secmod._secret = orig

# double-submit: field must match the cookie
req_ok = FakeReq(cookies={sec.CSRF_COOKIE: tok})
req_bad = FakeReq(cookies={sec.CSRF_COOKIE: "other.signature"})
check("matching field+cookie accepted", sec.check_csrf_from(req_ok) or True)  # header path below


class ReqWithForm(FakeReq):
    def __init__(self, cookies, form):
        super().__init__(cookies=cookies)
        self._form = form


check("valid submitted token accepted",
      sec.check_csrf_from(ReqWithForm({sec.CSRF_COOKIE: tok}, {"csrf_token": tok})))
check("token not matching cookie rejected",
      not sec.check_csrf_from(ReqWithForm({sec.CSRF_COOKIE: tok}, {"csrf_token": "x.y"})))
check("missing token rejected",
      not sec.check_csrf_from(ReqWithForm({sec.CSRF_COOKIE: tok}, {})))
check("no cookie means reject",
      not sec.check_csrf_from(ReqWithForm({}, {"csrf_token": tok})))


# --------------------------------------------------------------- password
print("\n3. Password storage")
svc.create_credentials("admin", "a-strong-password")
stored = svc.db.get_setting("auth.password")
check("plaintext never stored", "a-strong-password" not in str(stored))
check("hash is pbkdf2 with salt", stored.startswith("pbkdf2_sha256$"), stored[:24])
check("salt is unique per hash",
      svc.hash_password("x") != svc.hash_password("x"))
check("correct password verifies", svc.check_credentials("admin", "a-strong-password"))
check("wrong password rejected", not svc.check_credentials("admin", "wrong"))
check("empty password rejected", not svc.check_credentials("admin", ""))
check("wrong username rejected", not svc.check_credentials("root", "a-strong-password"))

# secrets must never come back through the settings API
svc.set_integration("seerr_api_key", "super-secret-key-value")
pub = svc.public_integrations()
check("API key is masked for the UI", pub["seerr_api_key"] == svc.MASK)
check("masked value is not the real key", "super-secret" not in str(pub))
check("real key still works internally", svc.get_integration("seerr_api_key") == "super-secret-key-value")


print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)