"""Helpers for short-lived FMS -> Cases launch-context tokens.

This is the signing foundation only. The Cases UI callback and relationship
endpoints must be installed alongside the corresponding Cases package/API
implementation before enabling this in production.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.config import settings

TOKEN_TTL_SECONDS = 300

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def create_fms_launch_token(context: dict[str, Any]) -> str:
    secret = (settings.fms_cases_shared_secret or "").strip()
    if len(secret) < 32:
        raise RuntimeError("FMS_CASES_SHARED_SECRET must be configured with at least 32 characters")
    now = int(time.time())
    claims = {
        "iss": "fms-portal",
        "aud": "cases-system",
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        **context,
    }
    encoded = _b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"

def sign_cases_request(method: str, path: str, timestamp: str, body: bytes = b"") -> str:
    """Build the HMAC signature for future server-to-server Cases requests."""
    secret = (settings.fms_cases_shared_secret or "").strip()
    if len(secret) < 32:
        raise RuntimeError("FMS_CASES_SHARED_SECRET is not configured")
    body_hash = hashlib.sha256(body).hexdigest()
    message = "\n".join([method.upper(), path, str(timestamp), body_hash])
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
