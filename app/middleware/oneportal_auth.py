from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import table_api
from app.api.client import ApiError
from app.auth.request_context import (
    AuthenticatedIdentity,
    reset_current_bearer_token,
    reset_current_identity,
    set_current_bearer_token,
    set_current_identity,
)
from app.auth.sso_session import SESSION_COOKIE, read_session_cookie
from app.config import settings


EXCLUDED_PATH_PREFIXES = (
    "/static",
    "/docs",
    "/redoc",
    "/openapi.json",
)

EXCLUDED_PATHS = {
    "/healthz",
    "/favicon.ico",
    "/auth/launch",
}


def _is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "y", "yes", "active"}


def _normalize_key(name: Any) -> str:
    return "".join(ch for ch in str(name or "").strip().lower() if ch.isalnum() or ch == "_")


def _pick_ci(row: dict[str, Any], *keys: str) -> Any:
    if not row:
        return None
    lookup = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        lk = str(key).strip().lower()
        if lk in lookup:
            return lookup[lk]
    return None


def _extract_bearer_token(request: Request) -> tuple[str | None, str]:
    auth_header = request.headers.get("Authorization") or ""
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
        if token:
            return token, "header"

    # Generic query-string handoff support from SSO launch URLs.
    for query_name in ("access_token", "token", "jwt", "oneportal_token"):
        token = str(request.query_params.get(query_name) or "").strip()
        if token:
            return token, "query"

    # Cookie fallbacks for browser-based SSO handoff.
    for cookie_name in (
        "oneportal_access_token",
        "oneportal_token",
        "access_token",
        "auth_token",
        "jwt",
        "token",
    ):
        token = str(request.cookies.get(cookie_name) or "").strip()
        if token:
            return token, "cookie"

    return None, "none"


def _extract_permissions(rows: list[dict[str, Any]]) -> set[str]:
    permissions: set[str] = set()
    structural_keys = {
        "roleid", "role_id", "employeecode", "employee_code", "userid", "user_id",
        "createdon", "created_on", "modifiedon", "modified_on", "active", "status",
        "id", "rowid", "row_id", "app_id", "appid",
    }

    for row in rows:
        for key, value in row.items():
            norm_key = _normalize_key(key)
            if not norm_key or norm_key in structural_keys:
                continue

            # Support explicit permission code/name style rows.
            if norm_key in {"permission", "permissionname", "permissioncode", "action"}:
                if value and str(value).strip() and str(value).strip() not in {"0", "false", "no"}:
                    permissions.add(_normalize_key(value))
                continue

            # Support column-style RBAC flags (CanView, can_edit, etc.).
            if _is_truthy(value):
                permissions.add(norm_key)

    return permissions


class OnePortalAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        enforce_auth = bool(settings.one_portal_auth_enabled)
        capture_identity = bool(settings.one_portal_identity_capture_enabled)

        if not enforce_auth and not capture_identity:
            return await call_next(request)

        path = request.url.path
        if path in EXCLUDED_PATHS or any(path.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
            return await call_next(request)

        if path == "/":
            return await call_next(request)

        # 1) Preferred: FMS session cookie created by /auth/launch from a signed
        #    One Portal ticket. It only carries employee_code + One Portal session_id;
        #    everything else is re-read from the database below on every request.
        token: str | None = None
        token_source = "none"
        jwt_payload: dict[str, Any] | None = None

        sso_claims = read_session_cookie(request.cookies.get(SESSION_COOKIE))
        if sso_claims:
            token_source = "sso_cookie"
            jwt_payload = {
                "session_id": sso_claims.get("session_id"),
                "employee_code": sso_claims.get("employee_code"),
            }
        else:
            # 2) Legacy: One Portal JWT in header / query / cookie.
            token, token_source = _extract_bearer_token(request)
            if not token:
                if enforce_auth:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Not signed in. Open this application from One Portal."},
                    )
                return await call_next(request)

            if not settings.jwt_secret_key:
                if enforce_auth:
                    return JSONResponse(status_code=503, content={"detail": "JWT secret is not configured"})
                return await call_next(request)

            try:
                jwt_payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
            except jwt.ExpiredSignatureError:
                if enforce_auth:
                    return JSONResponse(status_code=401, content={"detail": "Token expired"})
                return await call_next(request)
            except jwt.InvalidTokenError:
                if enforce_auth:
                    return JSONResponse(status_code=401, content={"detail": "Invalid token"})
                return await call_next(request)

        session_id = str(jwt_payload.get("session_id") or jwt_payload.get("SessionID") or "").strip()
        if not session_id:
            if enforce_auth:
                return JSONResponse(status_code=401, content={"detail": "Invalid token structure"})
            return await call_next(request)

        try:
            session_rows = await table_api.get_table("TM_Login_Sessions", filters={"session_id": session_id})
        except ApiError:
            if enforce_auth:
                return JSONResponse(status_code=503, content={"detail": "Auth backend unavailable"})
            return await call_next(request)
        session_row = session_rows[0] if session_rows else None
        if not session_row:
            if enforce_auth:
                return JSONResponse(status_code=401, content={"detail": "Session not found"})
            return await call_next(request)

        session_status = _pick_ci(session_row, "Status", "status")
        if str(session_status or "").strip().lower() != "active":
            if enforce_auth:
                return JSONResponse(status_code=401, content={"detail": "Session is inactive"})
            return await call_next(request)

        expiry_raw = _pick_ci(session_row, "ExpiryTime", "expiry_time")
        if expiry_raw:
            try:
                expiry_str = str(expiry_raw).replace("T", " ").split(".")[0]
                expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expiry_dt:
                    if enforce_auth:
                        return JSONResponse(status_code=401, content={"detail": "Session expired"})
                    return await call_next(request)
            except ValueError:
                pass

        employee_code = str(
            _pick_ci(session_row, "EmployeeCode", "employee_code")
            or jwt_payload.get("employee_code")
            or ""
        ).strip()
        if not employee_code:
            if enforce_auth:
                return JSONResponse(status_code=401, content={"detail": "EmployeeCode missing in active session"})
            return await call_next(request)

        try:
            employee_rows = await table_api.get_table("VM_EmployeeMaster", filters={"EmployeeCode": employee_code})
        except ApiError:
            if enforce_auth:
                return JSONResponse(status_code=503, content={"detail": "Auth backend unavailable"})
            return await call_next(request)
        employee_row = employee_rows[0] if employee_rows else None
        if not employee_row:
            if enforce_auth:
                return JSONResponse(status_code=401, content={"detail": "Employee profile not found"})
            return await call_next(request)

        active_status = _pick_ci(employee_row, "Active", "active", "ActiveStatus", "active_status")
        if active_status is not None and not _is_truthy(active_status):
            if enforce_auth:
                return JSONResponse(status_code=401, content={"detail": "Employee is inactive"})
            return await call_next(request)

        role_id = str(_pick_ci(employee_row, "RoleID", "role_id") or "").strip()
        role_name = str(_pick_ci(employee_row, "RoleName", "role_name") or "").strip()
        user_id = str(_pick_ci(session_row, "UserID", "user_id") or "").strip()

        try:
            perm_rows = await table_api.get_table(
                "TM_User_Access_Master",
                filters={"RoleID": role_id, "EmployeeCode": employee_code},
            )
            if not perm_rows and role_id:
                perm_rows = await table_api.get_table("TM_User_Access_Master", filters={"RoleID": role_id})
        except ApiError:
            perm_rows = []

        permissions = _extract_permissions(perm_rows)

        identity = AuthenticatedIdentity(
            session_id=session_id,
            employee_code=employee_code,
            employee_name=str(_pick_ci(employee_row, "EmployeeName", "employee_name") or "").strip(),
            role_id=role_id,
            role_name=role_name,
            department=str(_pick_ci(employee_row, "Department", "department") or "").strip(),
            position=str(_pick_ci(employee_row, "Position", "position") or "").strip(),
            email=str(_pick_ci(employee_row, "Email", "OffEmail", "email") or "").strip(),
            mobile=str(_pick_ci(employee_row, "Mobile", "OffMobile", "mobile") or "").strip(),
            user_id=user_id,
            permissions=permissions,
            raw_employee=employee_row,
        )

        request.state.session_id = session_id
        request.state.employee_code = identity.employee_code
        request.state.employee_name = identity.employee_name
        request.state.role_id = identity.role_id
        request.state.role_name = identity.role_name
        request.state.department = identity.department
        request.state.position = identity.position
        request.state.email = identity.email
        request.state.mobile = identity.mobile
        request.state.user_id = identity.user_id
        request.state.permissions = permissions

        identity_token = set_current_identity(identity)
        bearer_token_token = set_current_bearer_token(token)  # None for SSO cookie -> API_AUTH_TOKEN is used

        try:
            response = await call_next(request)
        finally:
            reset_current_identity(identity_token)
            reset_current_bearer_token(bearer_token_token)

        # Persist query-based SSO token so identity remains available on subsequent navigations.
        if token_source == "query":
            response.set_cookie(
                key="oneportal_access_token",
                value=token,
                httponly=True,
                secure=(request.url.scheme == "https"),
                samesite="lax",
                max_age=8 * 60 * 60,
            )

        return response
