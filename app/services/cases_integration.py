"""Helpers for short-lived FMS <-> Cases launch-context tokens and relationship queries.

Provides secure HMAC token signing & verification, case location resolution
(Live / Snooze / Archive), and database-aware relationship management.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

from app.config import settings
from app.api import table_api
from app.auth.request_context import get_current_database_target

TOKEN_TTL_SECONDS = 300


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode((encoded + padding).encode("ascii"))


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


def verify_fms_launch_token(token: str) -> dict[str, Any]:
    secret = (settings.fms_cases_shared_secret or "").strip()
    if len(secret) < 32:
        raise RuntimeError("FMS_CASES_SHARED_SECRET must be configured with at least 32 characters")
    if not token or "." not in token:
        raise ValueError("Malformed launch token")

    encoded, signature = token.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid token signature")

    claims = json.loads(_b64url_decode(encoded).decode("utf-8"))
    now = int(time.time())
    if int(claims.get("exp", 0)) < now:
        raise ValueError("Launch token expired")
    if claims.get("iss") != "fms-portal" or claims.get("aud") != "cases-system":
        raise ValueError("Invalid token issuer or audience")
    return claims


def sign_cases_request(method: str, path: str, timestamp: str, body: bytes = b"") -> str:
    """Build the HMAC signature for server-to-server Cases requests."""
    secret = (settings.fms_cases_shared_secret or "").strip()
    if len(secret) < 32:
        raise RuntimeError("FMS_CASES_SHARED_SECRET is not configured")
    body_hash = hashlib.sha256(body).hexdigest()
    message = "\n".join([method.upper(), path, str(timestamp), body_hash])
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def compute_case_location_from_records(
    sa_records: list[dict[str, Any]],
    employee_code: str = "",
) -> str:
    """Determine case location: 'Live', 'Snooze', or 'Archive'.

    Reuses exact Cases System business rules:
    - Normal user: only their own EmployeeCode record.
    - Archive takes priority over expired Snooze.
    - If Snooze DateTime is in future -> 'Snooze'.
    - If Snooze DateTime is in past / expired -> 'Live'.
    - If neither -> 'Live'.
    """
    clean_emp = str(employee_code or "").strip().lower()

    user_records: list[dict[str, Any]] = []
    if clean_emp:
        user_records = [
            r for r in sa_records
            if str(r.get("EmployeeCode", "")).strip().lower() == clean_emp
        ]

    # If no record for specific employee, check general records
    records_to_inspect = user_records if user_records else sa_records
    if not records_to_inspect:
        return "Live"

    # Sort by ArchiveDateTime or SnoozeDateTime descending if available
    latest_archive: dict[str, Any] | None = None
    latest_snooze: dict[str, Any] | None = None

    for r in records_to_inspect:
        rec_type = str(r.get("Type") or r.get("type") or "").strip().lower()
        if rec_type == "archive":
            latest_archive = r
        elif rec_type == "snooze":
            latest_snooze = r

    if latest_archive:
        return "Archive"

    if latest_snooze:
        snooze_val = (
            latest_snooze.get("SnoozeDateTime")
            or latest_snooze.get("SnoozeDateTime")
            or latest_snooze.get("snoozeDateTime")
            or latest_snooze.get("SnoozeTime")
        )
        if snooze_val:
            try:
                # Format: 2026-11-04 15:30:00 or ISO
                s_str = str(snooze_val).replace("T", " ").split(".")[0].strip()
                dt = datetime.strptime(s_str, "%Y-%m-%d %H:%M:%S")
                if dt > datetime.now():
                    return "Snooze"
            except Exception:
                # If cannot parse, consider active if marked Snooze
                return "Snooze"

    return "Live"


async def get_linked_cases_for_form(
    app_id: str,
    task_id: str,
    stage_no: str = "",
    employee_code: str = "",
) -> list[dict[str, Any]]:
    """Retrieve all linked cases for a form and calculate their live locations."""
    clean_app = str(app_id or "").strip()
    clean_task = str(task_id or "").strip()
    clean_stage = str(stage_no or "").strip()

    if not clean_app or not clean_task:
        return []

    cases_by_id: dict[str, dict[str, Any]] = {}
    cases_base = settings.cases_system_url.rstrip("/")

    # 1. Query FMS_Case_Link locally via table_api
    try:
        filters: dict[str, Any] = {
            "FMSTaskID": clean_task,
        }
        if clean_app:
            filters["FMSAppID"] = clean_app

        # First attempt with stage filter if provided
        links: list[dict[str, Any]] = []
        if clean_stage:
            try:
                links = await table_api.get_table("FMS_Case_Link", filters={**filters, "FMSStageNo": clean_stage})
            except Exception:
                links = []

        if not links:
            links = await table_api.get_table("FMS_Case_Link", filters=filters)

        for link in (links or []):
            cid = str(link.get("CaseID", "")).strip()
            if cid and cid not in cases_by_id:
                cases_by_id[cid] = {
                    "case_id": cid,
                    "stage_no": link.get("FMSStageNo", clean_stage),
                    "stage_name": link.get("FMSStageName", f"Stage {clean_stage}" if clean_stage else ""),
                    "form_name": link.get("FMSFormName", "Linked FMS Form"),
                    "linked_at": str(link.get("LinkedAt", "")),
                    "open_url": f"{cases_base}?caseId={quote(cid, safe='')}",
                }
    except Exception as exc:
        print(f"[get_linked_cases_for_form] local FMS_Case_Link note: {exc}")

    # 2. Query Cases System server directly for cases associated with this task ID
    try:
        import httpx
        cs_url = f"{cases_base}/api/fms/cases-for-task"
        params = {"task_id": clean_task, "app_id": clean_app, "stage_no": clean_stage}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(cs_url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                cs_cases = data.get("cases", [])
                for c in cs_cases:
                    cid = str(c.get("case_id", "")).strip()
                    if cid:
                        if cid not in cases_by_id:
                            cases_by_id[cid] = c
                        else:
                            for k in ("subject", "created_by", "created_on", "location"):
                                if c.get(k) and not cases_by_id[cid].get(k):
                                    cases_by_id[cid][k] = c[k]
    except Exception as exc:
        print(f"[get_linked_cases_for_form] Cases System cases-for-task note: {exc}")

    if not cases_by_id:
        return []

    results: list[dict[str, Any]] = []

    for case_id, item in cases_by_id.items():
        case_subject = item.get("subject") or "-"
        created_by = item.get("created_by") or "-"
        created_on = item.get("created_on") or "-"
        if case_subject == "-" or created_by == "-":
            try:
                case_rows = await table_api.get_table("Case_Master", filters={"CaseID": case_id})
                if case_rows:
                    case_row = case_rows[0]
                    case_subject = str(case_row.get("Subject") or "-").strip()
                    created_by = str(case_row.get("CreatedBy") or "-").strip()
                    created_on = str(case_row.get("Timestamp") or "-").strip()
            except Exception as exc:
                print(f"[get_linked_cases_for_form] Failed to fetch Case_Master for {case_id}: {exc}")

        location = item.get("location") or "Live"
        try:
            sa_rows = await table_api.get_table("Case_SnoozeArchive", filters={"CaseID": case_id})
            location = compute_case_location_from_records(sa_rows or [], employee_code=employee_code)
        except Exception as exc:
            print(f"[get_linked_cases_for_form] Failed to fetch Case_SnoozeArchive for {case_id}: {exc}")

        open_url = item.get("open_url") or f"{cases_base}?caseId={quote(case_id, safe='')}"

        results.append({
            "case_id": case_id,
            "subject": case_subject,
            "location": location,
            "created_by": created_by,
            "created_on": created_on,
            "stage_no": item.get("stage_no", clean_stage),
            "stage_name": item.get("stage_name", f"Stage {clean_stage}" if clean_stage else ""),
            "form_name": item.get("form_name", "Linked FMS Form"),
            "open_url": open_url,
            "linked_at": item.get("linked_at", ""),
        })

    return results
