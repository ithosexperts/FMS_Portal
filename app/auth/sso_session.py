"""One Portal -> FMS single sign-on helpers.

Flow
----
1. One Portal (logged-in user) asks its own backend for a 45-second ticket.
2. The browser POSTs that ticket to FMS ``/auth/launch`` (inside the iframe).
3. FMS verifies signature / audience / expiry / one-time ``jti`` and sets its
   own HttpOnly session cookie that contains ONLY employee_code + the One
   Portal session_id.
4. On every request the middleware re-checks that One Portal session in
   TM_Login_Sessions, so logging out of One Portal also locks FMS.

The browser never gets to say who it is: no e-mail, name or employee code is
accepted from the URL, form fields or headers.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import jwt

from app.config import settings

PORTAL_ISSUER = "one-portal"
TICKET_AUDIENCE = "fms-portal"
SESSION_ISSUER = "fms-portal"
SESSION_AUDIENCE = "fms-session"
ALGORITHM = "HS256"
SESSION_COOKIE = "fms_sso_session"


class SsoError(Exception):
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


def _secret() -> str:
    secret = (settings.fms_sso_secret or "").strip()
    if len(secret) < 32:
        raise SsoError("FMS_SSO_SECRET is not configured (minimum 32 characters).", 503)
    return secret


def decode_ticket(ticket: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            ticket,
            _secret(),
            algorithms=[ALGORITHM],
            issuer=PORTAL_ISSUER,
            audience=TICKET_AUDIENCE,
            options={"require": ["exp", "iat", "jti", "employee_code", "portal_session_id"]},
            leeway=3,
        )
    except jwt.ExpiredSignatureError as exc:
        raise SsoError("Launch ticket expired. Please reopen FMS from One Portal.") from exc
    except jwt.InvalidTokenError as exc:
        raise SsoError("Invalid launch ticket.") from exc


class _ReplayStore:
    """Single-use ticket ids. In-memory => fine for ONE instance.

    If FMS runs on more than one instance/worker, replace with Redis or a SQL
    table, otherwise the same ticket could be accepted once per instance.
    """

    def __init__(self) -> None:
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def consume(self, jti: str, exp: int) -> bool:
        now = int(time.time())
        with self._lock:
            for key, expiry in list(self._used.items()):
                if expiry < now:
                    self._used.pop(key, None)
            if jti in self._used:
                return False
            self._used[jti] = max(exp, now + 60)
            return True


replay_store = _ReplayStore()


def make_session_cookie_value(
    employee_code: str,
    portal_session_id: str,
    ttl_seconds: int,
    target_database: str | None = None,
) -> str:
    now = int(time.time())
    target = str(target_database or "").strip()
    if target not in {"Test_OperationsDB", "OperationsDB"}:
        target = "OperationsDB" if settings.is_production else "Test_OperationsDB"
    return jwt.encode(
        {
            "iss": SESSION_ISSUER,
            "aud": SESSION_AUDIENCE,
            "sub": employee_code,
            "employee_code": employee_code,
            "session_id": portal_session_id,
            "target_database": target,
            "iat": now,
            "nbf": now - 1,
            "exp": now + ttl_seconds,
        },
        _secret(),
        algorithm=ALGORITHM,
    )


def read_session_cookie(value: str | None) -> dict[str, Any] | None:
    """Return claims, or None for a missing / invalid / expired cookie."""
    value = (value or "").strip()
    if not value or len((settings.fms_sso_secret or "").strip()) < 32:
        return None
    try:
        return jwt.decode(
            value,
            settings.fms_sso_secret.strip(),
            algorithms=[ALGORITHM],
            issuer=SESSION_ISSUER,
            audience=SESSION_AUDIENCE,
            options={"require": ["exp", "employee_code", "session_id"]},
            leeway=3,
        )
    except jwt.InvalidTokenError:
        return None


def safe_next_path(value: str | None, default: str = "/dashboard") -> str:
    """Only allow same-site relative paths (blocks open-redirects)."""
    path = (value or "").strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or path.startswith("/auth/")
        or any(ch in path for ch in "\r\n")
    ):
        return default
    return path
