
"""
Generic async HTTP client shared by every API module.

This is the ONLY place that ever issues an HTTP request to API_1 / API_2.
Services and routers never call `httpx` directly - they call the small
wrapper functions in api/*.py, which in turn call this client.
"""
from __future__ import annotations

import logging
from typing import Any


import httpx

from app.auth.request_context import get_current_bearer_token, get_current_identity
from app.api.endpoints import API_ENDPOINTS
from app.config import settings

logger = logging.getLogger("fms_portal.api")


def _current_role_id() -> str:
    identity = get_current_identity()
    if not identity:
        return ""
    return str(getattr(identity, "role_id", "") or "").strip()


def _select_base_for_role(spec_base: str) -> str:
    """Select the database API from deployment environment, not employee role.

    Local/development runs against Test_OperationsDB (API_1).
    Production runs against OperationsDB (API_2).

    This keeps the exact same form/SP code in both environments while ensuring
    metadata, table reads, updates and stored procedures all hit the same DB.
    RoleID must never decide which database is used; it only controls permissions.
    """
    # Optional explicit override for controlled testing.
    target = str(getattr(settings, "fms_db_target", "") or "").strip().upper()
    if target in {"TEST", "API_1", "TESTING"}:
        return "API_1"
    if target in {"PROD", "PRODUCTION", "API_2", "LIVE"}:
        return "API_2"

    # Default: environment determines the database.
    return "API_2" if settings.is_production else "API_1"


class ApiError(Exception):
    """Raised whenever a call to API_1/API_2 fails or returns a non-2xx status."""

    def __init__(self, message: str, status_code: int | None = None, endpoint: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint


def _base_url(base_key: str) -> str:
    if base_key == "API_1":
        return settings.api_1_url.rstrip("/")
    if base_key == "API_2":
        return settings.api_2_url.rstrip("/")
    raise ApiError(f"Unknown API base '{base_key}'")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    bearer_token = get_current_bearer_token()
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    elif settings.api_auth_token:
        headers["Authorization"] = f"Bearer {settings.api_auth_token}"
    return headers


def resolve_endpoint(endpoint_key: str, **path_params: Any) -> tuple[str, str]:
    """Return (http_method, full_url) for a logical endpoint key."""
    if endpoint_key not in API_ENDPOINTS:
        raise ApiError(f"Unknown endpoint key '{endpoint_key}'")

    spec = API_ENDPOINTS[endpoint_key]
    selected_base = _select_base_for_role(spec["base"])

    path = spec["path"].format(**path_params)
    url = f"{_base_url(selected_base)}{path}"
    logger.debug(
        "Resolved route: endpoint=%s role=%s base=%s url=%s",
        endpoint_key,
        _current_role_id() or "Guest",
        selected_base,
        url,
    )
    return spec["method"], url


class ApiClient:
    """Thin async wrapper around httpx, driven entirely by endpoints.py."""

    def __init__(self, timeout: float | None = None):
        self.timeout = timeout or settings.request_timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = __import__("asyncio").Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 5.0)),
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0),
                    headers={"Connection": "keep-alive"},
                )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        endpoint_key: str,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        method, url = resolve_endpoint(endpoint_key, **(path_params or {}))

        try:
            client = await self._get_client()
            response = await client.request(
                    method,
                    url,
                    params=query_params,
                    json=json_body,
                headers=_headers(),
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = exc.response.text.strip()[:4000]
            except Exception:
                body = ""
            message = str(exc)
            if body:
                message = f"{message}; response_body={body}"
            logger.error("API %s -> %s failed: %s", endpoint_key, url, message)
            raise ApiError(message, status_code=exc.response.status_code, endpoint=endpoint_key) from exc
        except httpx.HTTPError as exc:
            logger.error("API %s -> %s unreachable: %s", endpoint_key, url, exc)
            raise ApiError(str(exc), endpoint=endpoint_key) from exc

    async def get(self, endpoint_key: str, path_params: dict[str, Any] | None = None,
                    query_params: dict[str, Any] | None = None) -> Any:
        return await self.request(endpoint_key, path_params=path_params, query_params=query_params)

    async def post(self, endpoint_key: str, path_params: dict[str, Any] | None = None,
                    query_params: dict[str, Any] | None = None,
                    json_body: dict[str, Any] | None = None) -> Any:
        return await self.request(
            endpoint_key, 
            path_params=path_params, 
            query_params=query_params, 
            json_body=json_body
        )


api_client = ApiClient()



