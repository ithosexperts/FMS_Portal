"""FMS Portal API router for FMS <-> Cases System Two-Way Integration.

Handles:
- Fetching linked cases and count for the current form.
- Generating signed FMS launch tokens for Case creation.
- Linking Cases directly to FMS forms.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.auth.request_context import (
    get_current_database_target,
    set_current_database_target,
    reset_current_database_target,
)
from app.api import table_api
from app.services import cases_integration

router = APIRouter(prefix="/api/fms/cases", tags=["FMS Cases Integration"])


class LaunchTokenRequest(BaseModel):
    app_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    stage_no: str = Field(default="", max_length=64)
    stage_name: str = Field(default="", max_length=255)
    form_name: str = Field(default="", max_length=255)
    form_url: str = Field(default="", max_length=1000)


class LinkCaseRequest(BaseModel):
    token: str = Field(min_length=20)
    case_id: str = Field(min_length=1, max_length=64)


@router.get("/linked")
async def get_linked_cases(
    request: Request,
    app_id: str = "",
    task_id: str = "",
    stage_no: str = "",
) -> dict[str, Any]:
    """Retrieve all cases linked to the current FMS form."""
    employee_code = str(getattr(request.state, "employee_code", "") or "").strip()
    cases = await cases_integration.get_linked_cases_for_form(
        app_id=app_id,
        task_id=task_id,
        stage_no=stage_no,
        employee_code=employee_code,
    )
    return {
        "status": "success",
        "count": len(cases),
        "cases": cases,
    }


@router.post("/launch-token")
async def create_launch_token(
    payload: LaunchTokenRequest,
    request: Request,
) -> dict[str, Any]:
    """Generate a signed HMAC launch token to create a new case from FMS."""
    secret = (settings.fms_cases_shared_secret or "").strip()
    if len(secret) < 32:
        raise HTTPException(
            status_code=500,
            detail="FMS_CASES_SHARED_SECRET is not configured or too short (minimum 32 characters)."
        )

    # Determine current database target (OperationsDB or Test_OperationsDB)
    target_database = get_current_database_target()
    if not target_database:
        target_database = "OperationsDB" if settings.is_production else "Test_OperationsDB"

    employee_code = str(getattr(request.state, "employee_code", "") or "").strip()

    context = {
        "app_id": payload.app_id.strip(),
        "task_id": payload.task_id.strip(),
        "stage_no": payload.stage_no.strip(),
        "stage_name": payload.stage_name.strip(),
        "form_name": payload.form_name.strip(),
        "form_url": payload.form_url.strip(),
        "database": target_database,
        "employee_code": employee_code,
    }

    token = cases_integration.create_fms_launch_token(context)
    cases_base = settings.cases_system_url.rstrip("/")
    launch_url = f"{cases_base}/create-case?fms_token={token}"

    return {
        "status": "success",
        "token": token,
        "launch_url": launch_url,
    }


@router.post("/link")
async def link_case_to_form(
    payload: LinkCaseRequest,
    request: Request,
) -> dict[str, Any]:
    """Direct relationship persist endpoint."""
    try:
        claims = cases_integration.verify_fms_launch_token(payload.token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid or expired launch token: {exc}")

    target_database = str(claims.get("database", "OperationsDB") or "OperationsDB").strip()
    case_id = payload.case_id.strip()

    db_token = set_current_database_target(target_database)
    try:
        # Check existing
        try:
            existing = await table_api.get_table("FMS_Case_Link", filters={
                "CaseID": case_id,
                "FMSAppID": claims["app_id"],
                "FMSTaskID": claims["task_id"],
                "FMSStageNo": claims.get("stage_no", "") or "",
                "DatabaseTarget": target_database,
            })
            if existing:
                return {"status": "success", "already_linked": True, "case_id": case_id}
        except Exception:
            pass

        # Insert
        link_data = {
            "CaseID": case_id,
            "FMSAppID": claims["app_id"],
            "FMSTaskID": claims["task_id"],
            "FMSStageNo": claims.get("stage_no", "") or "",
            "FMSStageName": claims.get("stage_name", "") or "",
            "FMSFormName": claims.get("form_name", "") or "",
            "FMSFormURL": claims.get("form_url", "") or "",
            "DatabaseTarget": target_database,
            "LinkedBy": claims.get("employee_code", ""),
            "LinkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        await table_api.insert_row("FMS_Case_Link", link_data)
    finally:
        reset_current_database_target(db_token)

    return {"status": "success", "message": "Case linked successfully.", "case_id": case_id}
