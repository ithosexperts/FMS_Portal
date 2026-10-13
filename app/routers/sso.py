"""POST /auth/launch - consume a One Portal ticket and start an FMS session."""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api import table_api
from app.api.client import ApiError
from app.auth.sso_session import (
    SESSION_COOKIE,
    SsoError,
    decode_ticket,
    make_session_cookie_value,
    replay_store,
    safe_next_path,
)
from app.config import settings
from app.auth.request_context import set_current_database_target, reset_current_database_target

logger = logging.getLogger("fms_portal.sso")
router = APIRouter()


def _error_page(message: str, status_code: int) -> HTMLResponse:
    body = (
        "<html><body style='font-family:Arial;padding:32px'>"
        "<h2>Unable to sign in to FMS</h2>"
        f"<p>{html.escape(message)}</p>"
        "<p>Please close this page and open FMS again from One Portal.</p>"
        "</body></html>"
    )
    return HTMLResponse(body, status_code=status_code)


def _pick(row: dict, *keys: str):
    lookup = {str(k).strip().lower(): v for k, v in (row or {}).items()}
    for key in keys:
        if key.lower() in lookup:
            return lookup[key.lower()]
    return None


@router.post("/auth/launch")
async def launch_from_one_portal(ticket: str = Form(...), next: str = Form("/dashboard")):
    try:
        claims = decode_ticket(ticket.strip())
    except SsoError as exc:
        return _error_page(str(exc), exc.status_code)

    if not replay_store.consume(str(claims["jti"]), int(claims.get("exp", 0))):
        return _error_page("This launch ticket was already used.", 401)

    employee_code = str(claims["employee_code"]).strip()
    portal_session_id = str(claims["portal_session_id"]).strip()
    target_database = str(claims.get("target_database") or "").strip()
    if target_database not in {"Test_OperationsDB", "OperationsDB"}:
        return _error_page("Launch ticket has an invalid database target.", 401)
    if not employee_code or not portal_session_id:
        return _error_page("Launch ticket is incomplete.", 401)

    # Validate the live One Portal session against the same DB selected in the
    # portal that minted this signed ticket.
    db_context = set_current_database_target(target_database)
    try:
        rows = await table_api.get_table("TM_Login_Sessions", filters={"session_id": portal_session_id})
    except ApiError:
        return _error_page("Login service is temporarily unavailable.", 503)
    finally:
        reset_current_database_target(db_context)
    row = rows[0] if rows else None
    if not row or str(_pick(row, "Status", "status") or "").strip().lower() != "active":
        return _error_page("Your One Portal session is no longer active.", 401)

    ttl = max(300, int(settings.sso_session_minutes) * 60)
    cookie_value = make_session_cookie_value(employee_code, portal_session_id, ttl, target_database)

    same_site = settings.sso_cookie_samesite.lower()
    if same_site not in {"lax", "strict", "none"}:
        same_site = "none"

    response = RedirectResponse(url=safe_next_path(next), status_code=303)
    cookie_secure = (same_site == "none") or settings.is_production
    response.set_cookie(
        key=SESSION_COOKIE,
        value=cookie_value,
        max_age=ttl,
        httponly=True,
        # SameSite=None (needed for a cross-site iframe) requires Secure.
        secure=cookie_secure,
        samesite=same_site,
        path="/",
    )
    # In browsers that block unpartitioned third-party cookies, the standard
    # cookie above may not be sent when FMS is embedded under One Portal. A
    # partitioned copy (CHIPS) supports that iframe session without moving the
    # browser's top-level address bar to the FMS origin. Both copies carry the
    # same signed value; first-party FMS use continues to work as before.
    if cookie_secure and same_site == "none":
        response.headers.append(
            "Set-Cookie",
            f"{SESSION_COOKIE}={cookie_value}; Max-Age={ttl}; Path=/; HttpOnly; Secure; SameSite=None; Partitioned",
        )
    logger.info("SSO launch ok employee=%s", employee_code)
    return response
