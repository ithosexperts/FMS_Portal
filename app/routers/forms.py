
"""
Independent form routes - Application Form (always read-only) and Stage
Form (editable only when it is the task's current stage). These never
share code with the dashboard.
"""
from __future__ import annotations

import os
import json
import re
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from app.config import settings
from app.api import table_api, task_api
from app.api.client import ApiError, api_client
from app.services import form_service, stage_service, task_service, stage1_custom_service
from app.templates_config import templates
from app.utils import route_builder

router = APIRouter(tags=["forms"])

LEAD_TIME_GRANT_TABLE_CANDIDATES = ("Z_Lead_Time_Grant", "Z_Lead_time_Grant")

# Set this from the actual vehicle master table name when it is provided.
# Keeping it configurable avoids hardcoding a table that may differ by environment.
VEHICLE_MASTER_TABLE = os.getenv("VEHICLE_MASTER_TABLE", "FL_Vehical").strip()
DRIVER_MASTER_TABLE = os.getenv("DRIVER_MASTER_TABLE", "FL_Users").strip()
VEHICLE_MASTER_NO_ALIASES = (
    "VehicleNo",
    "Vehicle_No",
    "Vehicle Number",
    "VehicleNumber",
    "REGISTRATION_NO",
    "RegistrationNo",
    "Registration Number",
    "RegNo",
    "Reg_No",
)


def _pick_vehicle_number(row: dict[str, Any]) -> str:
    normalized = {"".join(ch for ch in str(k).lower() if ch.isalnum()): v for k, v in row.items()}
    for alias in VEHICLE_MASTER_NO_ALIASES:
        key = "".join(ch for ch in alias.lower() if ch.isalnum())
        value = normalized.get(key)
        if value is not None and str(value).strip() and str(value).strip().lower() != "none":
            return str(value).strip()
    return ""


async def _load_vehicle_master_options() -> list[str]:
    """Load vehicle numbers for Own Vehicle from the configured vehicle master table.

    The table name is intentionally configurable because the actual master-table name
    will be supplied separately. Until then the Own Vehicle dropdown remains safe and
    simply has no options rather than querying an incorrect table.
    """
    if not VEHICLE_MASTER_TABLE:
        return []

    try:
        rows = await table_api.get_table(VEHICLE_MASTER_TABLE)
    except Exception as exc:
        print(f"Failed to load vehicle master '{VEHICLE_MASTER_TABLE}': {exc}")
        return []

    values: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        number = _pick_vehicle_number(row)
        if not number:
            continue
        key = number.upper()
        if key in seen:
            continue
        seen.add(key)
        values.append(number)

    values.sort(key=str.upper)
    return values


DRIVER_NAME_ALIASES = (
    "DriverName",
    "Name",
    "FullName",
    "UserName",
    "Username",
    "EmployeeName",
    "Employee_Name",
    "Employee Name",
    "User Full Name",
    "DisplayName",
)
DRIVER_PHONE_ALIASES = (
    "Phone",
    "PhoneNo",
    "PhoneNumber",
    "Mobile",
    "MobileNo",
    "MobileNumber",
    "ContactNo",
    "ContactNumber",
    "DriverContact",
    "Contact",
)
DRIVER_ROLE_ALIASES = (
    "Role",
    "UserRole",
    "RoleName",
    "User_Role",
    "UserType",
    "User_Type",
    "Designation",
)

def _clean_10_digit_mobile(value: Any) -> str:
    digits = re.sub(r"\D", "", _to_text(value))
    return digits if re.fullmatch(r"\d{10}", digits) else ""

def _pick_driver_name(row: dict[str, Any]) -> str:
    normalized = {"".join(ch for ch in str(k).lower() if ch.isalnum()): v for k, v in row.items()}
    for alias in DRIVER_NAME_ALIASES:
        key = "".join(ch for ch in alias.lower() if ch.isalnum())
        value = normalized.get(key)
        if value is not None and _to_text(value):
            return _to_text(value)
    # Heuristic fallback for differently named name columns.
    for raw_key, value in row.items():
        key = "".join(ch for ch in str(raw_key).lower() if ch.isalnum())
        if ("name" in key or key in {"fullname", "employeename"}) and "username" not in key:
            text = _to_text(value)
            if text:
                return text
    return ""

def _pick_driver_phone(row: dict[str, Any]) -> str:
    normalized = {"".join(ch for ch in str(k).lower() if ch.isalnum()): v for k, v in row.items()}
    for alias in DRIVER_PHONE_ALIASES:
        key = "".join(ch for ch in alias.lower() if ch.isalnum())
        value = normalized.get(key)
        mobile = _clean_10_digit_mobile(value)
        if mobile:
            return mobile
    # Heuristic fallback for differently named mobile/contact columns.
    for raw_key, value in row.items():
        key = "".join(ch for ch in str(raw_key).lower() if ch.isalnum())
        if any(token in key for token in ("mobile", "phone", "contact")):
            mobile = _clean_10_digit_mobile(value)
            if mobile:
                return mobile
    return ""

def _pick_driver_role(row: dict[str, Any]) -> str:
    normalized = {"".join(ch for ch in str(k).lower() if ch.isalnum()): v for k, v in row.items()}
    for alias in DRIVER_ROLE_ALIASES:
        key = "".join(ch for ch in alias.lower() if ch.isalnum())
        value = normalized.get(key)
        if value is not None:
            return _to_text(value)
    # Heuristic fallback for role-like columns.
    for raw_key, value in row.items():
        key = "".join(ch for ch in str(raw_key).lower() if ch.isalnum())
        if "role" in key or "designation" in key or "usertype" in key:
            return _to_text(value)
    return ""

async def _load_driver_master_options() -> list[dict[str, str]]:
    """Load DRIVER users from FL_Users where the role/designation is DRIVER."""
    if not DRIVER_MASTER_TABLE:
        return []
    try:
        rows = await table_api.get_table(DRIVER_MASTER_TABLE)
    except Exception as exc:
        print(f"Failed to load driver master '{DRIVER_MASTER_TABLE}': {exc}")
        return []

    print(f"[S2 DRIVER MASTER] table={DRIVER_MASTER_TABLE} rows={len(rows or [])}")
    values: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        role = _pick_driver_role(row).strip().casefold()
        if role != "driver":
            continue
        name = _pick_driver_name(row)
        mobile = _pick_driver_phone(row)
        if not name or not mobile:
            continue
        key = _normalize_key(name)
        if key in seen:
            continue
        seen.add(key)
        values.append({"name": name, "mobile": mobile})
    values.sort(key=lambda item: item["name"].casefold())
    print(f"[S2 DRIVER MASTER] driver options={len(values)}")
    return values

async def _load_publish_names(table_name: str) -> dict[str, str]:
    """Load user-facing Publish Name values from TM_Form_Settings.

    TM_Form_Settings column names can vary slightly by environment, so the
    lookup is deliberately tolerant. Only non-empty/non-placeholder Publish
    Names are used; otherwise the form keeps its safe fallback label.
    """
    if not table_name:
        return {}

    def norm(value: Any) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

    try:
        rows = await table_api.get_table(
            "TM_Form_Settings",
            filters={"TABLE_NAME": table_name, "fetchAll": "true"},
        )
    except Exception as exc:
        print(f"[Publish Name] Failed to load TM_Form_Settings for {table_name}: {exc}")
        return {}

    out: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue

        field_name = None
        publish_name = None
        for key, value in row.items():
            k = norm(key)
            if k in {"field", "fieldname", "column", "columnname", "dbfield", "dbname", "name"} and field_name is None:
                field_name = value
            elif k in {"publishname", "publish", "displayname", "caption", "label"} and publish_name is None:
                publish_name = value

        if field_name is None or publish_name is None:
            continue
        field_key = norm(field_name)
        publish_text = str(publish_name).strip()
        if not field_key or not publish_text:
            continue
        if publish_text.lower() in {"default name...", "default name", "null", "none"}:
            continue
        out[field_key] = publish_text

    return out


def _publish_label(publish_names: dict[str, str], field_name: str, fallback: str) -> str:
    key = "".join(ch for ch in str(field_name or "").lower() if ch.isalnum())
    return publish_names.get(key) or fallback



def _clean_display_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if text.casefold() in {"", "none", "null"}:
        return None
    return value


def _stage456_value(row: dict[str, Any], stage_no: str, suffixes: tuple[str, ...], aliases: tuple[str, ...] = ()) -> Any:
    stage_norm = _normalize_key(stage_no)
    candidates = tuple(suffixes) + tuple(aliases)
    # Prefer an exact stage-prefixed field, then a direct alias.
    for key in row.keys():
        kn = _normalize_key(key)
        if stage_norm and stage_norm in kn and any(kn.endswith(_normalize_key(s)) for s in candidates):
            value = _row_value_ci(row, key)
            if not is_blank_for_form(value):
                return value
    for wanted in candidates:
        value = _row_value_ci(row, wanted)
        if not is_blank_for_form(value):
            return value
    return None


def is_blank_for_form(value: Any) -> bool:
    return value is None or str(value).strip().casefold() in {"", "none", "null"}


async def _load_pack_dispatch_lines_for_stage456(stage_context: dict[str, Any]) -> list[dict[str, Any]]:
    row = stage_context.get("row") or {}
    fms = stage_context.get("fms") or {}
    table_name = str(fms.get("table_name") or "").strip()
    if table_name.casefold() != "z_salesorder":
        return [row] if row else []

    doc_num = _to_text(_pick_value_by_suffix(row, ("idocnum", "docnum", "salesorderno")))
    create_ts = _to_text(_pick_value_by_suffix(row, ("icreatets", "createts", "createtimestamp", "createdts")))
    if not doc_num:
        return [row] if row else []

    rows: list[dict[str, Any]] = []
    for filters in (
        {"I_DocNum": doc_num, "I_CreateTS": create_ts} if create_ts else {"I_DocNum": doc_num},
        {"DocNum": doc_num, "CreateTS": create_ts} if create_ts else {"DocNum": doc_num},
        {"I_DocNum": doc_num},
        {"DocNum": doc_num},
    ):
        try:
            rows = await table_api.get_table("Z_SalesOrder", filters=filters)
        except Exception as exc:
            print(f"[S4/S5/S6] Failed loading Z_SalesOrder with filters={filters}: {exc}")
            rows = []
        if rows:
            break

    if create_ts and rows:
        same_ts = []
        for item in rows:
            item_ts = _to_text(_pick_value_by_suffix(item, ("icreatets", "createts", "createtimestamp", "createdts")))
            if not item_ts or item_ts == create_ts:
                same_ts.append(item)
        rows = same_ts or rows

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row_id = _to_text(_row_value_ci(item, "RowID") or _row_value_ci(item, "ID") or _row_value_ci(item, "PK"))
        key = row_id or repr(sorted((str(k), str(v)) for k, v in item.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def _build_stage456_custom_context(stage_context: dict[str, Any], stage_no: str) -> dict[str, Any]:
    stage_no = str(stage_no or "").strip().upper()
    row = stage_context.get("row") or {}
    fms = stage_context.get("fms") or {}
    table_name = str(fms.get("table_name") or "").strip()
    publish_names = await _load_publish_names(table_name)

    mapping = [
        ("PLHelper", "PLHelper", "Planned Helper"),
        ("PLBaseTS", "PLBaseTS", "Base TS"),
        ("LeadTimeMins", "LeadTimeMins", "Lead Time Mins"),
        ("PLTimeStamp", "PLTimeStamp", "Planned Timestamp"),
        ("Doer", "Doer", "Doer"),
        ("ALHelper", "ALHelper", "Actual Helper"),
        ("LTGrantMins", "LTGrantMins", "Lead Time Grant Min"),
        ("DelayMins", "DelayMins", "Delay Time"),
        ("ALTimeStamp", "ALTimeStamp", "Actual Timestamp"),
        ("TaskStatus", "TaskStatus", "Task Status"),
    ]

    summary: list[dict[str, Any]] = []
    for logical, suffix, fallback_caption in mapping:
        aliases = {
            "LTGrantMins": ("LeadTimeGrantMins", "LeadTimeGrantMin"),
            "DelayMins": ("DelayTime", "DelayMins"),
            "ALTimeStamp": ("ActualTimestamp", "ActualDateTime"),
            "TaskStatus": ("Status",),
        }.get(logical, ())
        value = _stage456_value(row, stage_no, (suffix,), aliases)
        summary.append({
            "field": f"{stage_no}_{suffix}",
            "caption": _publish_label(publish_names, f"{stage_no}_{suffix}", f"{stage_no} {fallback_caption}"),
            "value": _clean_display_value(value),
        })

    context: dict[str, Any] = {
        "enabled": True,
        "stage_no": stage_no,
        "summary": summary,
        "publish_names": publish_names,
        # S4/S5/S6 custom forms are view-only as requested. Lead Time Extension
        # remains controlled by context.editable, not by this display lock.
        "stage_locked": True,
        "sales_order_no": _to_text(_pick_value_by_suffix(row, ("idocnum", "docnum", "salesorderno"))),
        "line_rows": [],
    }

    if stage_no == "S4":
        context["invoice_doc_no"] = {
            "field": "S4_InvDocNo",
            "caption": _publish_label(publish_names, "S4_InvDocNo", "S4 Invoice Doc No"),
            "value": _clean_display_value(_row_value_ci(row, "S4_InvDocNo") or _row_value_ci(row, "S4_InvoiceDocNo")),
        }
        context["invoice_line_no"] = {
            "field": "S4_InvLineNo",
            "caption": _publish_label(publish_names, "S4_InvLineNo", "S4 Invoice Line No"),
            "value": _clean_display_value(_row_value_ci(row, "S4_InvLineNo") or _row_value_ci(row, "S4_InvoiceLineNo")),
        }
        return context

    if stage_no == "S5":
        context["returned_field_name"] = "S5_ReturnedToStore"
        context["returned_caption"] = _publish_label(publish_names, "S5_ReturnedToStore", "Returned to Store")
        context["unpacked_field_name"] = "S5_MaterialUnpacked"
        context["unpacked_caption"] = _publish_label(publish_names, "S5_MaterialUnpacked", "Material Unpacked")
        for item in await _load_pack_dispatch_lines_for_stage456(stage_context):
            row_id = _to_text(_row_value_ci(item, "RowID") or _row_value_ci(item, "ID") or _row_value_ci(item, "PK"))
            if not row_id:
                continue
            item_name = _to_text(_row_value_ci(item, "I_ItemName") or _row_value_ci(item, "ItemName"))
            desc = _to_text(_row_value_ci(item, "I_Dscription") or _row_value_ci(item, "I_Description") or _row_value_ci(item, "Dscription"))
            context["line_rows"].append({
                "row_id": row_id,
                "item_name": item_name or desc or "-",
                "quantity": _row_value_ci(item, "I_Quantity") or _row_value_ci(item, "Quantity"),
                "returned_value": _row_value_ci(item, "S5_ReturnedToStore") or _row_value_ci(item, "S5_Retunrned to store"),
                "unpacked_value": _row_value_ci(item, "S5_MaterialUnpacked") or _row_value_ci(item, "S5_MatrialUnpacked"),
            })
        return context

    # S6 user fields: expose stage-prefixed fields from the loaded row, with
    # Publish Name labels, while remaining read-only.
    excluded = {
        "S6_PLHelper", "S6_PLBaseTS", "S6_LeadTimeMins", "S6_PLTimeStamp", "S6_Doer",
        "S6_ALHelper", "S6_ALTimeStamp", "S6_DelayMins", "S6_TaskStatus", "S6_KPIApplicable",
    }
    user_fields = []
    for key, value in row.items():
        key_text = str(key)
        if not key_text.startswith("S6_") or key_text in excluded:
            continue
        user_fields.append({
            "field": key_text,
            "caption": _publish_label(publish_names, key_text, key_text),
            "input_type": "text",
            "value": _clean_display_value(value),
        })
    user_fields.sort(key=lambda item: item["caption"].casefold())
    context["user_fields"] = user_fields
    return context


def _extract_stage_no_int(stage_no: str) -> int:
    match = re.search(r"\d+", str(stage_no or ""))
    if not match:
        return 0
    return int(match.group(0))


def _pick_stage_doer_value(stage_context: dict[str, Any]) -> str:
    stage_no = str((stage_context.get("stage") or {}).get("STAGE_NO") or "")
    custom_ctx = stage_context.get("custom_stage1") or {}
    top_fields = custom_ctx.get("top_fields") if isinstance(custom_ctx, dict) else None
    doer_field = top_fields.get("doer") if isinstance(top_fields, dict) else None

    value = getattr(doer_field, "value", None) if doer_field else None
    if value is not None and str(value).strip() and str(value).strip().lower() != "none":
        return str(value).strip()

    suffixes = ("doer",)
    for q in stage_context.get("fields", []):
        field_name = str(getattr(q, "field", "") or "")
        normalized = "".join(ch for ch in field_name.lower() if ch.isalnum())
        if not any(normalized.endswith(s) for s in suffixes):
            continue
        stage_key = "".join(ch for ch in stage_no.lower() if ch.isalnum())
        if stage_key and stage_key not in normalized:
            continue
        q_val = getattr(q, "value", None)
        if q_val is not None and str(q_val).strip() and str(q_val).strip().lower() != "none":
            return str(q_val).strip()
    return ""


def _find_stage_field_name(stage_context: dict[str, Any], stage_no: str, *suffixes: str) -> str | None:
    stage_key = _normalize_key(stage_no)
    fallback = None
    for q in stage_context.get("fields", []):
        field_name = str(getattr(q, "field", "") or "")
        if not field_name:
            continue
        normalized = _normalize_key(field_name)
        if not any(normalized.endswith(suffix) for suffix in suffixes):
            continue
        if stage_key and stage_key in normalized:
            return field_name
        if fallback is None:
            fallback = field_name
    return fallback


def _detect_stage_custom_template(table_name: str, stage_no: str) -> str | None:
    """Auto-detect custom stage templates by common naming conventions."""
    safe_table = str(table_name or "").strip()
    safe_stage = str(stage_no or "").strip()
    if not safe_stage:
        return None

    candidates = [
        f"custom_forms/{safe_table}_stage_{safe_stage}.html",
        f"custom_forms/{safe_table}_{safe_stage}.html",
        f"custom_forms/stage_{safe_stage}.html",
    ]

    for rel in candidates:
        abs_path = os.path.join("app", "templates", *rel.split("/"))
        if os.path.exists(abs_path):
            return rel
    return None


def _normalize_sql_datetime(value: Any) -> Any:
    """Normalize HTML datetime-local / ISO values for SQL Server datetime columns."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None

    # datetime-local sends YYYY-MM-DDTHH:MM; accept seconds/microseconds too.
    candidates = (
        text,
        text.replace("Z", "+00:00"),
    )
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            # Z/offset is not stored with SQL Server datetime here; preserve the
            # local wall-clock value and format it explicitly for SQL Server.
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    # Already SQL-like strings sometimes arrive with a space.
    if "T" in text:
        text = text.replace("T", " ", 1)
    return text


def _same_saved_value(expected: Any, actual: Any, field_name: str = "") -> bool:
    if expected in (None, ""):
        return actual in (None, "") or str(actual).strip().lower() in {"none", "null", ""}
    if "datetime" in str(field_name).lower():
        exp = _normalize_sql_datetime(expected)
        act = _normalize_sql_datetime(actual)
        return str(exp or "").strip() == str(act or "").strip()
    return str(actual or "").strip() == str(expected).strip()


async def _build_stage2_custom_context(stage_context: dict[str, Any]) -> dict[str, Any]:
    row = stage_context.get("row") or {}
    fields = stage_context.get("fields") or []
    table_name = str((stage_context.get("fms") or {}).get("table_name") or "")
    publish_names = await _load_publish_names(table_name)

    def _normalize_key(value: Any) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

    def _find_field(*suffixes: str):
        for q in fields:
            field_name = str(getattr(q, "field", "") or "").strip()
            if not field_name:
                continue
            normalized = _normalize_key(field_name)
            if any(normalized.endswith(_normalize_key(suffix)) for suffix in suffixes):
                return q
        return None

    def _find_row_value(*suffixes: str):
        for key in row.keys():
            normalized = _normalize_key(key)
            if any(normalized.endswith(_normalize_key(suffix)) for suffix in suffixes):
                return row.get(key)
        return None

    def _find_stage1_row_value(*suffixes: str):
        """Prefer S1_* values so Stage 2 inherits the Stage 1 source data."""
        normalized_suffixes = tuple(_normalize_key(s) for s in suffixes)

        # 1) Explicit S1-prefixed columns.
        for key in row.keys():
            normalized = _normalize_key(key)
            if not normalized.startswith("s1"):
                continue
            if any(normalized.endswith(suffix) for suffix in normalized_suffixes):
                return row.get(key)

        # 2) Fallback for legacy/non-prefixed source columns, but never prefer S2_*.
        for key in row.keys():
            normalized = _normalize_key(key)
            if normalized.startswith("s2"):
                continue
            if any(normalized.endswith(suffix) for suffix in normalized_suffixes):
                return row.get(key)
        return None

    def _find_row_value_for_field(field_name: str):
        target = _normalize_key(field_name)
        # Exact normalized match first (e.g. S2_Vehicle_Origin).
        for key, row_value in row.items():
            if _normalize_key(key) == target:
                return row_value
        # Then accept legacy variants that end with the same normalized field name.
        for key, row_value in row.items():
            key_norm = _normalize_key(key)
            if key_norm.endswith(target):
                return row_value
        return None

    def _field_payload(field_name: str, fallback_label: str, options: list[str] | None = None):
        field_obj = _find_field(field_name, field_name.replace("_", ""), field_name.lower())
        if field_obj is None:
            field_obj = _find_field(f"s2_{field_name}", f"{field_name}")

        field_value = getattr(field_obj, "value", None) if field_obj else None
        row_value = _find_row_value_for_field(field_name)
        # The saved task-row value is the source of truth for reopening an existing
        # Stage 2 form. Fall back to the field object only when the row has no value.
        value = row_value if row_value is not None and str(row_value).strip().lower() not in ("", "none") else field_value

        return {
            "field": getattr(field_obj, "field", field_name) if field_obj else field_name,
            "label": getattr(field_obj, "caption", fallback_label) if field_obj else fallback_label,
            "value": value,
            "options": options or [],
        }

    def _readonly_stage1_payload(label: str, *suffixes: str):
        value = _find_stage1_row_value(*suffixes)
        return {
            "label": label,
            "value": value,
        }

    def _blank_if_null(value: Any):
        return None if value is None or str(value).strip().lower() in ("", "none", "null") else value

    # Stage 2 header: ONLY Stage 2 columns.  Do not fall back to S1 here.
    # NULL/blank values are intentionally preserved as blank in the UI.
    stage2_summary = [
        {"label": _publish_label(publish_names, "S2_PLHelper", "S2 Planned Helper"), "value": _blank_if_null(_find_row_value("s2_plhelper"))},
        {"label": _publish_label(publish_names, "S2_PLBaseTS", "S2 Base TS"), "value": _blank_if_null(_find_row_value("s2_plbasets"))},
        {"label": _publish_label(publish_names, "S2_LeadTimeMins", "S2 Lead Time Mins"), "value": _blank_if_null(_find_row_value("s2_leadtimemins"))},
        {"label": _publish_label(publish_names, "S2_PLTimeStamp", "S2 Planned Timestamp"), "value": _blank_if_null(_find_row_value("s2_pltimestamp", "s2_pltime"))},
        {"label": _publish_label(publish_names, "S2_Doer", "S2 Doer"), "value": _blank_if_null(_find_row_value("s2_doer", "s2_doerempcode", "s2_doer_code"))},
        {"label": _publish_label(publish_names, "S2_ALHelper", "S2 Actual Helper"), "value": _blank_if_null(_find_row_value("s2_alhelper"))},
        {"label": _publish_label(publish_names, "S2_DelayMins", "S2 Delay Mins"), "value": _blank_if_null(_find_row_value("s2_delaymins", "s2_delaytime"))},
        {"label": _publish_label(publish_names, "S2_ALTimeStamp", "S2 Actual Timestamp"), "value": _blank_if_null(_find_row_value("s2_altimestamp", "s2_actualtimestamp", "s2_actualdatetime", "s2_altime"))},
        {"label": _publish_label(publish_names, "S2_KPIApplicable", "S2 KPI Applicable"), "value": _blank_if_null(_find_row_value("s2_kpiapplicable", "s2_kpiapp"))},
        {"label": _publish_label(publish_names, "S2_TaskStatus", "S2 Task Status"), "value": _blank_if_null(_find_row_value("s2_taskstatus"))},
    ]

    # Stage 2 is editable while the task is pending.  AL helper/timestamp are
    # not completion indicators by themselves because the downstream workflow
    # may populate those before the stage is actually closed.  Lock only when
    # the task status explicitly indicates completion/closure.
    s2_actual_ts = _blank_if_null(_find_row_value("s2_altimestamp", "s2_actualtimestamp", "s2_actualdatetime", "s2_altime"))
    s2_task_status = _blank_if_null(_find_row_value("s2_taskstatus"))
    s2_actual_helper = _blank_if_null(_find_row_value("s2_alhelper"))
    status_norm = _normalize_key(s2_task_status)
    completed_statuses = {
        "done", "donedelay", "completed", "complete", "closed", "closedelay",
    }
    s2_locked = bool(status_norm in completed_statuses)

    # Retained only for backwards-compatible template context. Stage 2's
    # editable Vehicle Origin field is always sourced from S2_VehicleOrigin.
    stage1_vehicle_origin = _find_stage1_row_value("s1_vehicleorigin", "s1_vehicle_origin")

    # Stage 1 packing inputs are inherited into Stage 2 and intentionally
    # rendered read-only. They are never submitted back as editable S2 fields.
    stage1_packing = {
        "no_packages": _readonly_stage1_payload(_publish_label(publish_names, "S1_NoOfPackages", "No of Packages"), "s1_noofpackages"),
        "weight_kgs": _readonly_stage1_payload(_publish_label(publish_names, "S1_WeightKgs", "Weight Kgs"), "s1_weightkgs", "s1_weightkg"),
        "remarks": _readonly_stage1_payload(_publish_label(publish_names, "S1_RemarksDoc", "Stage 1 Remarks"), "s1_remarksdoc", "s1_remarks"),
        "packing_img_1": _readonly_stage1_payload(_publish_label(publish_names, "S1_PackImage1", "Packing Img1"), "s1_packimage1", "s1_packingimg1", "s1_packingimage1"),
    }

    driver_master_options = await _load_driver_master_options()

    # Resolve PickID robustly. Some API responses may omit S1_PickID from the
    # initially loaded task row even though the physical Z_SalesOrder row has it.
    # When that happens, use the task RowID/primary key to retrieve the row and
    # recover the same PickID so the S1 PickNPack button can always open.
    resolved_pick_id = _to_text(_find_row_value_for_field("S1_PickID") or _find_row_value_for_field("PickID"))
    if not resolved_pick_id and table_name.lower() == "z_salesorder":
        source_row_id = _to_text(
            _find_row_value("RowID", "rowid", "ID", "id", "PK", "pk")
            or stage_context.get("task_id")
        )
        if source_row_id:
            try:
                for filter_key in ("RowID", str(stage_context.get("primary_key") or "").strip()):
                    if not filter_key:
                        continue
                    try:
                        candidate_rows = await table_api.get_table("Z_SalesOrder", filters={filter_key: source_row_id})
                    except Exception:
                        candidate_rows = []
                    for candidate in candidate_rows or []:
                        if not isinstance(candidate, dict):
                            continue
                        candidate_pick = _to_text(_pick_value_by_suffix(candidate, ("s1pickid", "pickid"), "S1"))
                        if candidate_pick:
                            resolved_pick_id = candidate_pick
                            break
                    if resolved_pick_id:
                        break
            except Exception as exc:
                print(f"[S2] PickID fallback lookup failed for RowID {source_row_id}: {exc}")

    current_driver_name = _to_text(_find_row_value_for_field("S2_AssignedDriver"))
    current_driver_number = _to_text(_find_row_value_for_field("S2_DriverContact"))
    if current_driver_name and _normalize_key(current_driver_name) not in {_normalize_key(d.get("name")) for d in driver_master_options}:
        driver_master_options = [{"name": current_driver_name, "mobile": _clean_10_digit_mobile(current_driver_number)}, *driver_master_options]

    # Normalize datetime-local values for an existing saved row.
    dispatch_payload = _field_payload("S2_DispatchDateTime", _publish_label(publish_names, "S2_DispatchDateTime", "Dispatch Date Time"))
    dispatch_value = dispatch_payload.get("value")
    if dispatch_value is not None:
        dispatch_text = str(dispatch_value).strip()
        dispatch_payload["value"] = dispatch_text.replace(" ", "T")[:16] if dispatch_text else dispatch_text

    return {
        "enabled": True,
        "stage2_summary": stage2_summary,
        "stage1_packing": stage1_packing,
        "stage1_vehicle_origin": stage1_vehicle_origin,
        "vehicle_origin": _field_payload("S2_VehicleOrigin", _publish_label(publish_names, "S2_VehicleOrigin", "Vehicle Origin"), ["Third Party", "Own Vehicle"]),
        "vehicle_type": _field_payload("S2_VehicleType", _publish_label(publish_names, "S2_VehicleType", "Vehicle Type"), []),
        "vehicle_no": _field_payload("S2_VehicleNo", _publish_label(publish_names, "S2_VehicleNo", "Vehicle No")),
        "driver_name": _field_payload("S2_AssignedDriver", _publish_label(publish_names, "S2_AssignedDriver", "Assigned Driver")),
        "driver_number": _field_payload("S2_DriverContact", _publish_label(publish_names, "S2_DriverContact", "Driver Contact")),
        "driver_master_options": driver_master_options,
        "driver_master_table": DRIVER_MASTER_TABLE,
        "pick_id": resolved_pick_id,
        "vehicle_master_options": [],
        "vehicle_master_table": VEHICLE_MASTER_TABLE,
        "loading_image_1": _field_payload("S2_LoadingImage1", _publish_label(publish_names, "S2_LoadingImage1", "Stage 2 Loading Image")),
        "dispatch_datetime": dispatch_payload,
        "remarks": _field_payload("S2_Remarks", _publish_label(publish_names, "S2_Remarks", "Stage 2 Remarks")),
        "s2_locked": s2_locked,
        "publish_names": publish_names,
    }



async def _build_stage3_custom_context(stage_context: dict[str, Any]) -> dict[str, Any]:
    """Build the custom Pack-to-Dispatch Stage 3 form context.

    Stage 3 keeps the S3 planning/procedure values in the summary, inherits
    the same Stage-1 PickNPack information as Stage 2, and exposes only the
    three Stage-3 user inputs: delivery document number, delivery line number
    and a multi-file packing-slip attachment.
    """
    row = stage_context.get("row") or {}
    fields = stage_context.get("fields") or []
    table_name = str((stage_context.get("fms") or {}).get("table_name") or "")
    publish_names = await _load_publish_names(table_name)

    def _normalize_key(value: Any) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

    def _find_row_value(*suffixes: str):
        normalized_suffixes = tuple(_normalize_key(s) for s in suffixes)
        # Exact normalized field match first.
        for key, value in row.items():
            key_norm = _normalize_key(key)
            if any(key_norm == suffix for suffix in normalized_suffixes):
                return value
        # Then suffix matching for legacy column naming variants.
        for key, value in row.items():
            key_norm = _normalize_key(key)
            if any(key_norm.endswith(suffix) for suffix in normalized_suffixes):
                return value
        return None

    def _find_stage1_row_value(*suffixes: str):
        normalized_suffixes = tuple(_normalize_key(s) for s in suffixes)
        for key, value in row.items():
            key_norm = _normalize_key(key)
            if not key_norm.startswith("s1"):
                continue
            if any(key_norm.endswith(suffix) for suffix in normalized_suffixes):
                return value
        for key, value in row.items():
            key_norm = _normalize_key(key)
            if key_norm.startswith("s3"):
                continue
            if any(key_norm.endswith(suffix) for suffix in normalized_suffixes):
                return value
        return None

    def _find_field(field_name: str):
        target = _normalize_key(field_name)
        for field_obj in fields:
            name = str(getattr(field_obj, "field", "") or "").strip()
            if _normalize_key(name) == target:
                return field_obj
        for field_obj in fields:
            name = str(getattr(field_obj, "field", "") or "").strip()
            if _normalize_key(name).endswith(target):
                return field_obj
        return None

    def _field_payload(field_name: str, fallback_label: str):
        field_obj = _find_field(field_name)
        row_value = _find_row_value(field_name)
        field_value = getattr(field_obj, "value", None) if field_obj else None
        value = row_value if row_value not in (None, "") else field_value
        return {
            "field": getattr(field_obj, "field", field_name) if field_obj else field_name,
            "label": getattr(field_obj, "caption", fallback_label) if field_obj else fallback_label,
            "value": value,
        }

    def _readonly(label: str, *suffixes: str):
        return {"label": label, "value": _find_stage1_row_value(*suffixes)}

    def _blank(value: Any):
        return None if value is None or str(value).strip().lower() in ("", "none", "null") else value

    stage3_summary = [
        {"label": _publish_label(publish_names, "S3_PLHelper", "S3 Planned Helper"), "value": _blank(_find_row_value("S3_PLHelper"))},
        {"label": _publish_label(publish_names, "S3_PLBaseTS", "S3 Base TS"), "value": _blank(_find_row_value("S3_PLBaseTS"))},
        {"label": _publish_label(publish_names, "S3_LeadTimeMins", "S3 Lead Time Mins"), "value": _blank(_find_row_value("S3_LeadTimeMins"))},
        {"label": _publish_label(publish_names, "S3_PLTimeStamp", "S3 Planned Timestamp"), "value": _blank(_find_row_value("S3_PLTimeStamp", "S3_PLTime"))},
        {"label": _publish_label(publish_names, "S3_Doer", "S3 Doer"), "value": _blank(_find_row_value("S3_Doer"))},
        {"label": _publish_label(publish_names, "S3_ALHelper", "S3 Actual Helper"), "value": _blank(_find_row_value("S3_ALHelper"))},
        {"label": _publish_label(publish_names, "S3_DelayMins", "S3 Delay Mins"), "value": _blank(_find_row_value("S3_DelayMins"))},
        {"label": _publish_label(publish_names, "S3_ALTimeStamp", "S3 Actual Timestamp"), "value": _blank(_find_row_value("S3_ALTimeStamp", "S3_ActualTimestamp", "S3_ActualDateTime", "S3_ALTime"))},
        {"label": _publish_label(publish_names, "S3_KPIApplicable", "S3 KPI Applicable"), "value": _blank(_find_row_value("S3_KPIApplicable", "S3_KPIApp"))},
        {"label": _publish_label(publish_names, "S3_TaskStatus", "S3 Task Status"), "value": _blank(_find_row_value("S3_TaskStatus"))},
    ]

    s3_status = _normalize_key(_find_row_value("S3_TaskStatus"))
    completed_statuses = {
        "done", "donedelay", "completed", "complete", "closed", "closedelay",
    }
    s3_locked = s3_status in completed_statuses

    resolved_pick_id = _to_text(_find_row_value("S1_PickID", "PickID"))
    if not resolved_pick_id and table_name.lower() == "z_salesorder":
        source_row_id = _to_text(
            _find_row_value("RowID", "ID", "PK") or stage_context.get("task_id")
        )
        if source_row_id:
            try:
                for filter_key in ("RowID", str(stage_context.get("primary_key") or "").strip()):
                    if not filter_key:
                        continue
                    try:
                        candidates = await table_api.get_table("Z_SalesOrder", filters={filter_key: source_row_id})
                    except Exception:
                        candidates = []
                    for candidate in candidates or []:
                        if not isinstance(candidate, dict):
                            continue
                        candidate_pick = _to_text(_pick_value_by_suffix(candidate, ("s1pickid", "pickid"), "S1"))
                        if candidate_pick:
                            resolved_pick_id = candidate_pick
                            break
                    if resolved_pick_id:
                        break
            except Exception as exc:
                print(f"[S3] PickID fallback lookup failed for RowID {source_row_id}: {exc}")

    return {
        "enabled": True,
        "stage3_summary": stage3_summary,
        "pick_id": resolved_pick_id,
        "stage1_packing": {
            "no_packages": _readonly(_publish_label(publish_names, "S1_NoOfPackages", "No of Packages"), "s1_noofpackages"),
            "weight_kgs": _readonly(_publish_label(publish_names, "S1_WeightKgs", "Weight Kgs"), "s1_weightkgs", "s1_weightkg"),
            "remarks": _readonly(_publish_label(publish_names, "S1_RemarksDoc", "Stage 1 Remarks"), "s1_remarksdoc", "s1_remarks"),
            "packing_img_1": _readonly(_publish_label(publish_names, "S1_PackImage1", "Packing Img1"), "s1_packimage1", "s1_packingimg1", "s1_packingimage1"),
        },
        "del_doc_no": _field_payload("S3_DelDocNo", "S3 Del Doc NO"),
        "del_line_no": _field_payload("S3_DelLineNo", "S3 Del Line NO"),
        "packing_slip": _field_payload("S3_PackingSlip", "S3 Packing Slip"),
        "s3_locked": s3_locked,
        "publish_names": publish_names,
    }


def _safe_stage2_fallback_context(stage_context: dict[str, Any], publish_names: dict[str, str] | None = None) -> dict[str, Any]:
    """Minimal, zero-failure Stage 2 context used if the richer builder fails.

    It reads existing saved values directly from the task row so reopening an
    existing Stage 2 can never turn a form-load problem into a 500 response.
    """
    row = stage_context.get("row") if isinstance(stage_context, dict) else {}
    if not isinstance(row, dict):
        row = {}
    publish_names = publish_names or {}

    def norm(value: Any) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

    def value_for(*names: str) -> Any:
        targets = {norm(name) for name in names if name}
        for key, value in row.items():
            if norm(key) in targets:
                return value
        for key, value in row.items():
            key_norm = norm(key)
            if any(key_norm.endswith(target) for target in targets):
                return value
        return None

    def text(value: Any) -> str:
        return "" if value is None else str(value).strip()

    origin = text(value_for("S2_Vehicle_Origin", "Vehicle_Origin", "VehicleOrigin"))
    vehicle_no = text(value_for("S2_Vehicle_No", "Vehicle_No", "VehicleNo"))
    driver_name = text(value_for("S2_AssignedDriver", "S2_DriverName", "DriverName"))
    driver_number = text(value_for("S2_DriverContact", "S2_DriverNumber", "DriverNumber", "DriverMobileNo"))
    options: list[str] = []
    driver_options: list[dict[str, str]] = []

    dispatch = text(value_for("S2_DispatchDateTime", "S2_Dispatch_Date_Time", "Dispatch_Date_Time", "DispatchDateTime"))
    if dispatch:
        dispatch = dispatch.replace(" ", "T")[:16]

    def readonly(label: str, *names: str) -> dict[str, Any]:
        return {"label": label, "value": value_for(*names)}

    def blank(value: Any):
        return None if value is None or str(value).strip().lower() in ("", "none", "null") else value

    stage2_summary = [
        {"label": _publish_label(publish_names, "S2_PLHelper", "S2 Planned Helper"), "value": blank(value_for("S2_PLHelper"))},
        {"label": _publish_label(publish_names, "S2_PLBaseTS", "S2 Base TS"), "value": blank(value_for("S2_PLBaseTS"))},
        {"label": _publish_label(publish_names, "S2_LeadTimeMins", "S2 Lead Time Mins"), "value": blank(value_for("S2_LeadTimeMins"))},
        {"label": _publish_label(publish_names, "S2_PLTimeStamp", "S2 Planned Timestamp"), "value": blank(value_for("S2_PLTimeStamp"))},
        {"label": _publish_label(publish_names, "S2_Doer", "S2 Doer"), "value": blank(value_for("S2_Doer"))},
        {"label": _publish_label(publish_names, "S2_ALHelper", "S2 Actual Helper"), "value": blank(value_for("S2_ALHelper"))},
        {"label": _publish_label(publish_names, "S2_DelayMins", "S2 Delay Mins"), "value": blank(value_for("S2_DelayMins"))},
        {"label": _publish_label(publish_names, "S2_ALTimeStamp", "S2 Actual Timestamp"), "value": blank(value_for("S2_ALTimeStamp"))},
        {"label": _publish_label(publish_names, "S2_KPIApplicable", "S2 KPI Applicable"), "value": blank(value_for("S2_KPIApplicable"))},
        {"label": _publish_label(publish_names, "S2_TaskStatus", "S2 Task Status"), "value": blank(value_for("S2_TaskStatus"))},
    ]

    return {
        "enabled": True,
        "stage2_summary": stage2_summary,
        "stage1_summary": [],
        "stage1_packing": {
            "no_packages": readonly(_publish_label(publish_names, "S1_NoOfPackages", "No of Packages"), "S1_NoOfPackages", "NoOfPackages"),
            "weight_kgs": readonly(_publish_label(publish_names, "S1_WeightKgs", "Weight Kgs"), "S1_WeightKgs", "WeightKgs", "WeightKg"),
            "remarks": readonly(_publish_label(publish_names, "S1_RemarksDoc", "Remarks"), "S1_Remarks", "S1_RemarksDoc", "RemarksDoc"),
            "packing_img_1": readonly(_publish_label(publish_names, "S1_PackImage1", "Packing Img1"), "S1_PackingImg1", "S1_PackingImage1", "PackingImg1", "PackingImage1"),
        },
        "stage1_vehicle_origin": readonly(_publish_label(publish_names, "S1_VehicleOrigin", "Vehicle Origin"), "S1_VehicleOrigin", "S1_Vehicle_Origin", "VehicleOrigin"),
        "vehicle_origin": {"field": "S2_VehicleOrigin", "label": _publish_label(publish_names, "S2_VehicleOrigin", "Vehicle Origin"), "value": origin, "options": ["Third Party", "Own Vehicle"]},
        "vehicle_type": {"field": "S2_VehicleType", "label": _publish_label(publish_names, "S2_VehicleType", "Vehicle Type"), "value": text(value_for("S2_VehicleType", "S2_Vehicle_Type", "Vehicle_Type", "VehicleType")), "options": []},
        "vehicle_no": {"field": "S2_VehicleNo", "label": _publish_label(publish_names, "S2_VehicleNo", "Vehicle No"), "value": vehicle_no, "options": []},
        "driver_name": {"field": "S2_AssignedDriver", "label": _publish_label(publish_names, "S2_AssignedDriver", "Assigned Driver"), "value": driver_name, "options": []},
        "driver_number": {"field": "S2_DriverContact", "label": _publish_label(publish_names, "S2_DriverContact", "Driver Contact"), "value": driver_number, "options": []},
        "driver_master_options": driver_options,
        "driver_master_table": DRIVER_MASTER_TABLE,
        "pick_id": text(value_for("S1_PickID", "PickID")),
        "vehicle_master_options": [],
        "vehicle_master_table": VEHICLE_MASTER_TABLE,
        "loading_image_1": {"field": "S2_LoadingImage1", "label": _publish_label(publish_names, "S2_LoadingImage1", "Stage 2 Loading Image"), "value": value_for("S2_LoadingImage1", "S2_Loading_Image1", "LoadingImage1")},
        "dispatch_datetime": {"field": "S2_DispatchDateTime", "label": _publish_label(publish_names, "S2_DispatchDateTime", "Dispatch Date Time"), "value": dispatch},
        "remarks": {"field": "S2_Remarks", "label": _publish_label(publish_names, "S2_Remarks", "Stage 2 Remarks"), "value": value_for("S2_Remarks", "Stage2Remarks")},
        "s2_locked": "".join(ch for ch in str(value_for("S2_TaskStatus") or "").lower() if ch.isalnum())
        in {"done", "donedelay", "completed", "complete", "closed", "closedelay"},
    }

def _get_authenticated_employee_code(request: Request) -> str:
    return str(getattr(request.state, "employee_code", "") or "").strip()


def _inject_update_audit_fields(payload: dict[str, Any], row: dict[str, Any], employee_code: str) -> None:
    if not employee_code or not isinstance(row, dict) or not row:
        return

    lowered = {str(k).lower(): str(k) for k in row.keys()}

    by_candidates = (
        "updatedbyemployeecode",
        "updated_by",
        "updatedby",
        "modified_by",
        "i_updated_by",
    )
    ts_candidates = (
        "updateddatetime",
        "updateddate",
        "updated_on",
        "modified_on",
        "i_updated_ts",
    )

    for candidate in by_candidates:
        if candidate in lowered:
            payload[lowered[candidate]] = employee_code
            break

    update_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for candidate in ts_candidates:
        if candidate in lowered:
            payload[lowered[candidate]] = update_ts
            break


def _normalize_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _row_value_ci(row: dict[str, Any], field_name: Any) -> Any:
    if not field_name:
        return None
    field = str(field_name)
    if field in row:
        return row[field]
    lowered = field.lower()
    compact = _normalize_key(lowered)
    for key, value in row.items():
        key_lower = str(key).lower()
        if key_lower == lowered or _normalize_key(key_lower) == compact:
            return value
    return None


def _row_value_by_suffix(row: dict[str, Any], suffixes: tuple[str, ...]) -> Any:
    best_key = None
    for key in row.keys():
        normalized = _normalize_key(key)
        if any(normalized.endswith(suffix) for suffix in suffixes):
            best_key = str(key)
            break
    return _row_value_ci(row, best_key)


def _pick_value_by_suffix(row: dict[str, Any], suffixes: tuple[str, ...], preferred_stage_prefix: str = "") -> Any:
    stage_key = _normalize_key(preferred_stage_prefix)
    fallback = None
    for key in row.keys():
        normalized = _normalize_key(key)
        if not any(normalized.endswith(suffix) for suffix in suffixes):
            continue
        if stage_key and stage_key in normalized:
            return _row_value_ci(row, key)
        if fallback is None:
            fallback = key
    return _row_value_ci(row, fallback)


def _to_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


async def _resolve_salesorder_row_ids_by_pick_id(stage_context: dict[str, Any], task_id: str) -> list[str]:
    """Return every Z_SalesOrder RowID carrying the same S1_PickID as this S2 form."""
    fms = stage_context.get("fms") or {}
    table_name = str(fms.get("table_name") or "").strip()
    if table_name.lower() != "z_salesorder":
        return [str(task_id)] if _to_text(task_id) else []

    row = stage_context.get("row") or {}
    pick_id = _to_text(_pick_value_by_suffix(row, ("s1pickid", "pickid"), "S1"))
    source_id = _to_text(_row_value_ci(row, stage_context.get("primary_key")) or _row_value_ci(row, "RowID") or task_id)
    if not pick_id:
        return [source_id] if source_id else []

    candidates: list[dict[str, Any]] = []
    for filters in ({"S1_PickID": pick_id}, {"PickID": pick_id}):
        try:
            rows = await table_api.get_table(table_name, filters=filters)
        except Exception:
            rows = []
        if rows:
            candidates.extend(rows)

    # Fallback to a full read if the gateway ignored the filters or the column
    # spelling differs.  We still filter strictly in Python by PickID.
    if not candidates:
        try:
            candidates = await table_api.get_table(table_name)
        except Exception:
            candidates = []

    target = _normalize_key(pick_id)
    ids: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_pick = _to_text(_pick_value_by_suffix(candidate, ("s1pickid", "pickid"), "S1"))
        if _normalize_key(candidate_pick) != target:
            continue
        candidate_id = _to_text(_row_value_ci(candidate, stage_context.get("primary_key")) or _row_value_ci(candidate, "RowID"))
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        ids.append(candidate_id)

    if source_id and source_id not in seen:
        ids.insert(0, source_id)
    return ids


async def _resolve_related_salesorder_row_ids(stage_context: dict[str, Any], task_id: str) -> list[str]:
    fms = stage_context.get("fms") or {}
    table_name = str(fms.get("table_name") or "").strip().lower()
    stage_no = str((stage_context.get("stage") or {}).get("STAGE_NO") or "").strip().lower()
    if table_name != "z_salesorder" or not stage_no.startswith("s1"):
        return []

    row = stage_context.get("row") or {}
    primary_key = str(stage_context.get("primary_key") or "").strip()

    source_row_id = _to_text(_row_value_ci(row, primary_key) or _row_value_ci(row, "RowID") or task_id)
    sales_order_no = _to_text(_row_value_by_suffix(row, ("idocnum", "docnum", "salesorderno")))
    create_ts = _to_text(_row_value_by_suffix(row, ("icreatets", "createts", "createtimestamp", "createdts")))

    if not sales_order_no:
        return []

    candidates: list[dict[str, Any]] = []
    for filters in ({"I_DocNum": sales_order_no}, {"DocNum": sales_order_no}):
        try:
            rows = await table_api.get_table(fms.get("table_name"), filters=filters)
        except Exception:
            rows = []
        if rows:
            candidates.extend(rows)

    related_ids: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_doc = _to_text(_row_value_by_suffix(candidate, ("idocnum", "docnum", "salesorderno")))
        candidate_create_ts = _to_text(_row_value_by_suffix(candidate, ("icreatets", "createts", "createtimestamp", "createdts")))
        if candidate_doc and candidate_doc != sales_order_no:
            continue
        if create_ts and candidate_create_ts and candidate_create_ts != create_ts:
            continue

        candidate_id = _to_text(_row_value_ci(candidate, primary_key) or _row_value_ci(candidate, "RowID"))
        if not candidate_id or candidate_id == source_row_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        related_ids.append(candidate_id)

    return related_ids


def _is_admin_role(request: Request) -> bool:
    # Clear & Reset is intentionally restricted to RoleID 1, 2, and 3 only.
    role_text = str(getattr(request.state, "role_id", "") or "").strip()
    return role_text in {"1", "2", "3"}


def _normalize_name(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _build_stage_clear_payload(stage_context: dict[str, Any], stage_no: str) -> dict[str, Any]:
    """Build the Clear & Reset payload without touching unrelated stages.

    Stage 3 is intentionally special: Clear & Reset must clear ONLY the four
    runtime/input fields below. Planning values, S3 delivery document/line,
    S4 values and all other data must remain untouched.
    """
    stage_key = _normalize_name(stage_no)

    if stage_key == "s3":
        return {
            "S3_PackingSlip": None,
            "S3_DelayMins": None,
            "S3_ALTimeStamp": None,
            "S3_TaskStatus": None,
        }

    stage = stage_context.get("stage") or {}
    fields = stage_context.get("fields") or []
    row = stage_context.get("row") or {}

    keep_suffixes = {
        "plhelper",
        "plbasets",
        "leadtimemins",
        "pltimestamp",
        "plannedtimestamp",
        "pltime",
        "doer",
        "doerempcode",
    }

    clear_payload: dict[str, Any] = {}
    for q in fields:
        field_name = str(getattr(q, "field", "") or "").strip()
        if not field_name:
            continue
        normalized = _normalize_name(field_name)
        if stage_key and stage_key not in normalized:
            continue
        if any(normalized.endswith(suffix) for suffix in keep_suffixes):
            continue
        clear_payload[field_name] = None

    actual_col = str(stage.get("ACTUAL_COLUMN") or "").strip()
    if actual_col:
        clear_payload[actual_col] = None

    force_clear_suffixes = (
        "altimestamp", "actualtimestamp", "actualdatetime", "altime",
        "alhelper", "delaymins", "delaymin", "delaytime", "taskstatus",
        "docentryso", "salesorderno", "triggersource", "rowid", "appid",
        "stageno", "doercode", "leadtimemins", "kpiapplicable",
        "kpiaapplicable", "kpia", "pltimestamp", "plannedtimestamp",
    )

    candidate_names: set[str] = set()
    for q in fields:
        name = str(getattr(q, "field", "") or "").strip()
        if name:
            candidate_names.add(name)
    for key in row.keys():
        name = str(key or "").strip()
        if name:
            candidate_names.add(name)

    for name in candidate_names:
        normalized = _normalize_name(name)
        if stage_key and stage_key not in normalized:
            continue
        if stage_key == "s1" and (
            "pltimestamp" in normalized or "pltime" in normalized
            or "kpiapplicable" in normalized or "kpiapp" in normalized
        ):
            continue
        if any(normalized.endswith(suffix) for suffix in force_clear_suffixes):
            clear_payload[name] = None

    if stage_key == "s2":
        clear_payload.update({
            "S2_VehicleOrigin": None,
            "S2_VehicleNo": None,
            "S2_LoadingImage1": None,
            "S2_Remarks": None,
            "S2_DispatchDateTime": None,
            "S2_VehicleType": None,
            "S2_AssignedDriver": None,
            "S2_DriverContact": None,
            "S2_ALHelper": None,
            "S2_ALTimeStamp": None,
            "S2_DelayMins": None,
            "S2_TaskStatus": None,
        })

    if stage_key == "s5":
        clear_payload.update({
            "S5_Retunrned to store": None,
            "S5_ReturnedToStore": None,
            "S5_ReturnedStore": None,
            "S5_MatrialUnpacked": None,
            "S5_MaterialUnpacked": None,
            "S5_ALHelper": None,
            "S5_ALTimeStamp": None,
            "S5_DelayMins": None,
            "S5_TaskStatus": None,
        })

    if stage_key == "s6":
        clear_payload.update({
            "S6_DocketNo": None,
            "S6_DocketDate": None,
            "S6_NoOfPackets": None,
            "S6_DocketCopy": None,
            "S6_BookTo": None,
            "S6_ALHelper": None,
            "S6_ALTimeStamp": None,
            "S6_DelayMins": None,
            "S6_TaskStatus": None,
        })

    return clear_payload


async def _resolve_lead_time_grant_table() -> str:
    for table_name in LEAD_TIME_GRANT_TABLE_CANDIDATES:
        try:
            await table_api.get_table(table_name, filters={"fetchAll": "false"})
            return table_name
        except ApiError:
            continue
    return LEAD_TIME_GRANT_TABLE_CANDIDATES[0]


@router.get("/forms/application/{task_id}", response_class=HTMLResponse)
async def application_form_page(request: Request, task_id: str):
    context = await task_service.get_task_context(task_id)
    table_name = context["fms"]["table_name"]
    fields = await form_service.build_application_form(table_name, context["row"])

    # Auto-detect Custom Form template
    custom_template_path = f"custom_forms/{table_name}.html"
    full_path = os.path.join("app", "templates", "custom_forms", f"{table_name}.html")
    has_custom_form = os.path.exists(full_path)

    return templates.TemplateResponse(request, "forms/application.html", {
        "task_id": task_id, 
        "context": context, 
        "fields": fields,
        "custom_form_template": custom_template_path if has_custom_form else None,
    })


@router.get("/forms/stage/{task_id}/{stage_no}", response_class=HTMLResponse)
async def stage_form_page(
    request: Request,
    task_id: str,
    stage_no: str,
    app_id: str = "",
    saved: bool = False,
    embedded: bool = False,
    pick_id: str = "",
    cleared: bool = False,
    appended: bool = False,
    ltg_saved: bool = False,
    ltg_error: str = "",
    clear_error: str = "",
):
    # When S1 PickNPack is opened from Stage 2, resolve the embedded S1 form
    # by the same PickID so the child form cannot accidentally open another
    # physical sales-order line.
    if embedded and str(stage_no).strip().upper() == "S1" and str(pick_id).strip():
        requested_pick_id = str(pick_id).strip()
        try:
            candidates = []
            for filter_key in ("S1_PickID", "PickID"):
                try:
                    candidates = await table_api.get_table(
                        "Z_SalesOrder",
                        filters={filter_key: requested_pick_id},
                    )
                except Exception:
                    candidates = []
                if candidates:
                    break

            if candidates:
                # Prefer the first row carrying the exact requested PickID.
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_pick = _pick_value_by_suffix(candidate, ("s1pickid", "pickid"), "S1")
                    if str(candidate_pick or "").strip() == requested_pick_id:
                        candidate_row_id = _row_value_ci(candidate, "RowID")
                        if candidate_row_id not in (None, ""):
                            task_id = str(candidate_row_id).strip()
                            break
        except Exception as exc:
            print(f"Embedded S1 PickID resolution failed for PickID {requested_pick_id}: {exc}")

    stage_context = await stage_service.get_stage_context(
        task_id, stage_no, app_id=(str(app_id).strip() or None)
    )
    table_name = str((stage_context.get("fms") or {}).get("table_name") or "")
    
    # Embedded S1 must be resolved BEFORE generic custom-template detection.
    # The Pack-to-Dispatch repository also contains custom S2/S3 templates, and
    # the generic detector can otherwise short-circuit the heavy S1 builder.
    # For embedded mode we deliberately use the dedicated S1 template with the
    # bare embedded layout and force the entire S1 form to read-only.
    if embedded and str(stage_no).strip().upper() == "S1":
        try:
            embedded_custom_stage1 = await stage1_custom_service.build_stage1_custom_context(stage_context)
        except Exception as exc:
            print(f"Embedded S1 custom context failed for task {task_id}/{stage_no}: {exc}")
            embedded_custom_stage1 = {"enabled": False}

        if embedded_custom_stage1.get("enabled"):
            embedded_custom_stage1["stage_locked"] = True
            embedded_custom_stage1["embedded"] = True
            return templates.TemplateResponse(request, "forms/stage_custom_s1.html", {
                "task_id": task_id,
                "stage_no": stage_no,
                "context": stage_context,
                "custom_stage1": embedded_custom_stage1,
                "has_pending_ltg": False,
                "current_role_id": str(getattr(request.state, "role_id", "") or "").strip(),
                "embedded": True,
                "layout_template": "forms/_embed_base.html",
            })

    # Custom Pack-to-Dispatch stages S4/S5/S6 are explicit: when the custom
    # HTML file exists, always use it and always build the custom context.
    stage_upper = str(stage_no or "").strip().upper()
    detected_custom_template = _detect_stage_custom_template(table_name, stage_no)
    if stage_upper in {"S4", "S5", "S6"} and str(table_name).strip().casefold() == "z_salesorder":
        explicit_template = f"custom_forms/stage_{stage_upper}.html"
        explicit_path = os.path.join("app", "templates", "custom_forms", f"stage_{stage_upper}.html")
        if os.path.exists(explicit_path):
            detected_custom_template = explicit_template
            print(
                "[CUSTOM FORM ROUTING] "
                f"task={task_id} stage={stage_upper} -> {explicit_template}"
            )

    custom_stage1: dict[str, Any] = {}
    custom_stage2: dict[str, Any] = {}
    custom_stage3: dict[str, Any] = {}
    custom_stage456: dict[str, Any] = {}

    if detected_custom_template:
        template_name = detected_custom_template
        if stage_upper in {"S4", "S5", "S6"}:
            try:
                custom_stage456 = await _build_stage456_custom_context(stage_context, stage_upper)
            except Exception as exc:
                print(f"[CUSTOM FORM CONTEXT] Stage {stage_upper} failed for task={task_id}: {exc!r}")
                custom_stage456 = {
                    "enabled": True,
                    "stage_no": stage_upper,
                    "summary": [],
                    "publish_names": {},
                    "stage_locked": True,
                    "sales_order_no": "",
                    "line_rows": [],
                }
                if stage_upper == "S4":
                    custom_stage456["invoice_doc_no"] = {"field": "S4_InvDocNo", "caption": "S4 Invoice Doc No", "value": None}
                    custom_stage456["invoice_line_no"] = {"field": "S4_InvLineNo", "caption": "S4 Invoice Line No", "value": None}
                elif stage_upper == "S5":
                    custom_stage456["returned_field_name"] = "S5_ReturnedToStore"
                    custom_stage456["returned_caption"] = "Returned to Store"
                    custom_stage456["unpacked_field_name"] = "S5_MaterialUnpacked"
                    custom_stage456["unpacked_caption"] = "Material Unpacked"
                else:
                    custom_stage456["user_fields"] = []
        elif stage_upper == "S2":
            try:
                custom_stage2 = await _build_stage2_custom_context(stage_context)
            except Exception as exc:
                print(f"Stage 2 custom context failed for task {task_id}/{stage_no}: {exc}")
                fallback_publish_names = await _load_publish_names(table_name)
                custom_stage2 = _safe_stage2_fallback_context(stage_context, fallback_publish_names)
        elif stage_upper == "S3":
            try:
                custom_stage3 = await _build_stage3_custom_context(stage_context)
            except Exception as exc:
                print(f"Stage 3 custom context failed for task {task_id}/{stage_no}: {exc}")
                custom_stage3 = {"enabled": False}
    else:
        custom_stage1 = await stage1_custom_service.build_stage1_custom_context(stage_context)
        if custom_stage1.get("enabled"):
            template_name = "forms/stage_custom_s1.html"
        else:
            template_name = "forms/stage.html"

    # =====================================================================
    # Check if any Lead Time Extension is already "Pending"
    # =====================================================================
    has_pending_ltg = False
    try:
        fms_name = str((stage_context.get("fms") or {}).get("fms_name") or "")
        stage_no_int = _extract_stage_no_int(stage_no)
        primary_key = str(stage_context.get("primary_key") or "").strip()
        row_data = stage_context.get("row") or {}
        fms_row_id = str(row_data.get(primary_key) or task_id or "").strip()

        lead_time_table = await _resolve_lead_time_grant_table()
        existing_requests = await table_api.get_table(
            lead_time_table,
            filters={
                "I_FMS_Name": fms_name,
                "I_Stage_No": stage_no_int,
                "I_FMS_Row_ID": fms_row_id,
                "I_Status": "Pending"
            }
        )
        if existing_requests and len(existing_requests) > 0:
            has_pending_ltg = True
    except Exception as e:
        print(f"Error checking pending ltg on load: {e}")
    # =====================================================================

    return templates.TemplateResponse(request, template_name, {
        "task_id": task_id, "stage_no": stage_no,
        "context": stage_context,
        "saved": saved,
        "cleared": cleared,
        "appended": appended,
        "ltg_saved": ltg_saved,
        "ltg_error": ltg_error,
        "clear_error": clear_error,
        "custom_stage1": custom_stage1,
        "custom_stage2": custom_stage2,
        "custom_stage3": custom_stage3,
        "custom_stage456": custom_stage456,
        "custom_stage_template": detected_custom_template,
        "has_pending_ltg": has_pending_ltg,
        "can_clear_responses": True,
        "current_role_id": str(getattr(request.state, "role_id", "") or "").strip(),
    })




def _parse_picker_list(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        values = [str(v).strip() for v in raw if str(v).strip()]
    else:
        values = [part.strip() for part in str(raw).split(",") if part.strip()]
    return list(dict.fromkeys(values))[:5]


def _norm_person(value: Any) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


async def _get_busy_picker_map(current_pick_id: str = "") -> dict[str, str]:
    rows = await stage1_custom_service.get_sales_order_picker_rows()
    busy: dict[str, str] = {}
    current_pick_norm = _norm_person(current_pick_id)
    for row in rows or []:
        row_pick_id = _to_text(_pick_value_by_suffix(row, ("s1pickid",), "S1"))
        # All rows carrying the current PickID are the same assignment and must
        # never make their own picker look busy to the current form.
        if current_pick_norm and _norm_person(row_pick_id) == current_pick_norm:
            continue
        actual = _pick_value_by_suffix(row, ("s1altimestamp", "s1altime", "s1actualtimestamp", "s1actualdatetime"), "S1")
        if actual not in (None, "") and str(actual).strip().lower() not in {"none", "null"}:
            continue
        pick_id = row_pick_id or _to_text(_pick_value_by_suffix(row, ("rowid", "id", "pk")))
        for picker in _parse_picker_list(_pick_value_by_suffix(row, ("s1picker",), "S1")):
            busy[_norm_person(picker)] = pick_id
    return busy


@router.get("/forms/stage/{task_id}/{stage_no}/picker-board")
async def stage1_picker_board(request: Request, task_id: str, stage_no: str, current_pick_id: str = ""):
    """Return the current list of pickers blocked by unfinished Stage 1 work."""
    stage_key = str(stage_no or "").strip().upper()
    if not (stage_key == "1" or stage_key.startswith("S1")):
        return JSONResponse(
            {"ok": False, "error": "Busy Picker board is only available for Stage 1."},
            status_code=400,
        )

    resolved_pick_id = str(current_pick_id or "").strip()
    if not resolved_pick_id:
        try:
            context = await stage_service.get_stage_context(task_id, stage_no)
            resolved_pick_id = _to_text(
                _pick_value_by_suffix(context.get("row") or {}, ("s1pickid", "pickid"), "S1")
            )
        except Exception:
            resolved_pick_id = ""

    try:
        assignments = await stage1_custom_service.get_busy_picker_assignments(resolved_pick_id)
    except Exception as exc:
        print(f"[Picker Board] Failed to load busy assignments: {exc}")
        return JSONResponse({"ok": False, "error": "Unable to load busy picker board."}, status_code=502)

    board_rows: list[dict[str, Any]] = []
    for assignment in assignments:
        row_links = [
            {
                "row_id": row_id,
                "stage_url": route_builder.stage_form_route(
                    row_id,
                    "S1",
                    "Z_SalesOrder",
                    "RowID",
                ),
            }
            for row_id in assignment.get("row_ids") or []
        ]
        board_rows.append({
            "pick_id": assignment.get("pick_id") or "-",
            "pickers": assignment.get("pickers") or [],
            "row_links": row_links,
        })

    busy_picker_names: list[str] = []
    seen_picker_keys: set[str] = set()
    for assignment in board_rows:
        for picker_name in assignment["pickers"]:
            key = _norm_person(picker_name)
            if key and key not in seen_picker_keys:
                seen_picker_keys.add(key)
                busy_picker_names.append(picker_name)

    busy_picker_names.sort(key=lambda value: value.casefold())
    return JSONResponse({
        "ok": True,
        "current_pick_id": resolved_pick_id,
        "busy_picker_names": busy_picker_names,
        "assignments": board_rows,
    })


@router.post("/forms/stage/{task_id}/{stage_no}/picker-save")
async def stage1_picker_save(request: Request, task_id: str, stage_no: str):
    # Accept the same Stage 1 route variants as the Weight endpoint.
    stage_key = str(stage_no or "").strip().upper()
    if not (stage_key == "1" or stage_key.startswith("S1")):
        return JSONResponse({"ok": False, "error": "Picker save is only available for Stage 1."}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    pickers = _parse_picker_list(body.get("pickers"))
    if len(pickers) > 5:
        return JSONResponse({"ok": False, "error": "Maximum 5 pickers allowed."}, status_code=400)

    context = await stage_service.get_stage_context(task_id, stage_no)
    if not context.get("editable"):
        return JSONResponse({"ok": False, "error": "This stage is not currently editable."}, status_code=409)

    row = context.get("row") or {}
    actual = _pick_value_by_suffix(row, ("s1altimestamp", "s1altime", "s1actualtimestamp", "s1actualdatetime"), "S1")
    if actual not in (None, "") and str(actual).strip().lower() not in {"none", "null"}:
        return JSONResponse({"ok": False, "error": "Stage 1 is already completed; picker cannot be edited."}, status_code=409)

    pick_id = _to_text(_pick_value_by_suffix(row, ("s1pickid",), "S1"))
    current_row_id = _to_text(_pick_value_by_suffix(row, ("rowid", "id", "pk"))) or _to_text(task_id)

    if pickers:
        busy_map = await _get_busy_picker_map(pick_id)
        conflicts = [(picker, busy_map[_norm_person(picker)]) for picker in pickers if _norm_person(picker) in busy_map]
        if conflicts:
            details = ", ".join(f"{name} is busy on PickID {pid}" for name, pid in conflicts)
            return JSONResponse({"ok": False, "error": details}, status_code=409)

    # Empty string is intentional when the last picker is removed. It is more
    # reliable through the SQL gateway than JSON null for clearing a varchar.
    value = ", ".join(pickers)
    saved_value = value

    # S1_Picker belongs to the PickID, not a single physical line. Persist the
    # same comma-separated value to every Z_SalesOrder row carrying this PickID.
    all_rows = await stage1_custom_service.get_sales_order_picker_rows()
    target_rows: list[str] = []
    pick_norm = _norm_person(pick_id)
    for sales_row in all_rows or []:
        sales_pick_id = _to_text(_pick_value_by_suffix(sales_row, ("s1pickid",), "S1"))
        sales_row_id = _to_text(_pick_value_by_suffix(sales_row, ("rowid", "id", "pk")))
        if sales_row_id and pick_norm and _norm_person(sales_pick_id) == pick_norm:
            target_rows.append(sales_row_id)
    if not target_rows:
        target_rows = [current_row_id]

    target_rows = list(dict.fromkeys(target_rows))
    update_errors: list[str] = []

    async def _write_picker_row(target_row_id: str) -> None:
        # Primary path: exact physical RowID update through the gateway.
        result = await api_client.post(
            "update_row",
            query_params={"action": "update", "table": "Z_SalesOrder"},
            json_body={
                "action": "update",
                "table": "Z_SalesOrder",
                "data": {"S1_Picker": saved_value},
                "where": {"RowID": target_row_id},
            },
        )
        if isinstance(result, dict) and str(result.get("status") or "").strip().lower() == "error":
            raise RuntimeError(str(result.get("error") or result.get("message") or "update failed"))

    for target_row_id in target_rows:
        try:
            await _write_picker_row(target_row_id)
        except Exception as exc:
            # Fallback through the generic table helper. This is important on
            # gateways where the explicit RowID contract is stricter than the
            # metadata-resolved primary-key path.
            try:
                await table_api.update_row("Z_SalesOrder", target_row_id, {"S1_Picker": saved_value})
            except Exception as fallback_exc:
                update_errors.append(f"{target_row_id}: {exc}; fallback: {fallback_exc}")

    if update_errors:
        return JSONResponse({
            "ok": False,
            "error": "Picker change could not be saved: " + "; ".join(update_errors),
        }, status_code=502)

    # Confirm each affected physical row. If the first path did not persist,
    # retry through the generic table helper once before failing the request.
    mismatches: list[str] = []
    for target_row_id in target_rows:
        try:
            refreshed = await table_api.get_row("Z_SalesOrder", target_row_id)
        except Exception as exc:
            mismatches.append(f"{target_row_id} ({exc})")
            continue
        persisted = _to_text(_pick_value_by_suffix(refreshed or {}, ("s1picker",), "S1"))
        if persisted != saved_value:
            try:
                await table_api.update_row("Z_SalesOrder", target_row_id, {"S1_Picker": saved_value})
                refreshed = await table_api.get_row("Z_SalesOrder", target_row_id)
                persisted = _to_text(_pick_value_by_suffix(refreshed or {}, ("s1picker",), "S1"))
            except Exception:
                pass
        if persisted != saved_value:
            mismatches.append(target_row_id)

    if mismatches:
        return JSONResponse({
            "ok": False,
            "error": "Picker change could not be confirmed for rows: " + ", ".join(mismatches),
        }, status_code=502)

    stage1_custom_service.invalidate_picker_assignment_cache()
    return JSONResponse({"ok": True, "value": saved_value, "pickers": pickers})


@router.post("/forms/stage/{task_id}/{stage_no}/weight-save")
async def stage1_weight_save(request: Request, task_id: str, stage_no: str):
    # The page router can pass Stage 1 as S1, 1, or a richer stage label
    # beginning with S1. Normalize that here so the dedicated Weight Save
    # endpoint never rejects a valid S1 form because of presentation text.
    stage_key = str(stage_no or "").strip().upper()
    if not (stage_key == "1" or stage_key.startswith("S1")):
        return JSONResponse({"ok": False, "error": "Weight save is only available for Stage 1."}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    row_id = _to_text(body.get("row_id"))
    raw_weight = _to_text(body.get("weight"))
    if not row_id or not re.fullmatch(r"\d+(?:\.\d{1,3})?", raw_weight):
        return JSONResponse({"ok": False, "error": "Enter a valid weight up to 3 decimal places."}, status_code=400)

    context = await stage_service.get_stage_context(task_id, stage_no)
    if not context.get("editable"):
        return JSONResponse({"ok": False, "error": "This stage is not currently editable."}, status_code=409)
    current_row = context.get("row") or {}
    actual = _pick_value_by_suffix(current_row, ("s1altimestamp", "s1altime", "s1actualtimestamp", "s1actualdatetime"), "S1")
    if actual not in (None, "") and str(actual).strip().lower() not in {"none", "null"}:
        return JSONResponse({"ok": False, "error": "Stage 1 is already completed; weight cannot be edited."}, status_code=409)

    try:
        update_result = await api_client.post(
            "update_row",
            query_params={
                "action": "update",
                "table": "Z_SalesOrder",
            },
            json_body={
                "action": "update",
                "table": "Z_SalesOrder",
                "data": {"S1_PickIDWeightkgs": raw_weight},
                "where": {"RowID": row_id},
            },
        )
        print(
            f"[S1 Weight Save] table=Z_SalesOrder RowID={row_id} "
            f"S1_PickIDWeightkgs={raw_weight!r} result={update_result}"
        )
        if isinstance(update_result, dict) and str(update_result.get("status", "")).strip().lower() == "error":
            raise RuntimeError(str(update_result.get("error") or update_result.get("message") or "Weight save failed."))
    except Exception as exc:
        try:
            await table_api.update_row("Z_SalesOrder", row_id, {"S1_PickIDWeightkgs": raw_weight})
        except Exception as fallback_exc:
            return JSONResponse({"ok": False, "error": f"Weight save failed: {exc}; fallback: {fallback_exc}"}, status_code=502)

    # Read back the exact Sales Order row to confirm the Save button really
    # persisted the value before the input is switched to read-only.
    refreshed_row = await table_api.get_row("Z_SalesOrder", row_id)
    if not isinstance(refreshed_row, dict):
        return JSONResponse({"ok": False, "error": "Weight was not saved: row could not be re-read."}, status_code=502)
    persisted = _to_text(_pick_value_by_suffix(refreshed_row, ("s1pickidweightkgs",), "S1"))
    try:
        persisted_num = float(persisted)
        expected_num = float(raw_weight)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Saved weight could not be confirmed."}, status_code=502)
    if abs(persisted_num - expected_num) > 0.000001:
        return JSONResponse({
            "ok": False,
            "error": f"Weight save could not be confirmed. Backend has '{persisted}'.",
        }, status_code=502)
    return JSONResponse({"ok": True, "row_id": row_id, "weight": f"{persisted_num:.3f}"})

@router.post("/forms/stage/{task_id}/{stage_no}", response_class=HTMLResponse)
async def stage_form_submit(request: Request, task_id: str, stage_no: str, app_id: str = ""):
    form_data = await request.form()
    requested_app_id = str(form_data.get("app_id") or app_id or "").strip()
    action = str(form_data.get("_action", "save") or "save").strip().lower()
    print(
        "[FORM SUBMIT] START: "
        f"task_id={task_id!r} stage={str(stage_no).strip().upper()!r} action={action!r}"
    )
    
    stage_context = await stage_service.get_stage_context(
        task_id, stage_no, app_id=(requested_app_id or None)
    )
    fms_info = stage_context.get("fms", {})
    
    app_id = fms_info.get("app_id", "GENERAL")
    current_year = datetime.now().strftime("%Y")
    attachment_folder_name = fms_info.get("attachment_folder") or f"{current_year}_{app_id}_ATTACHMENTS"

    upload_base_dir = Path("app/static/uploads") / attachment_folder_name
    upload_base_dir.mkdir(parents=True, exist_ok=True)

    raw_batch_payload = form_data.get("__batch_payload_json")

    payload = {}

    existing_attachment_map: dict[str, str] = {}
    for key in form_data.keys():
        if not key.startswith("__existing_attachment_urls_"):
            continue
        field_name = key.replace("__existing_attachment_urls_", "", 1)
        existing_attachment_map[field_name] = str(form_data.get(key) or "").strip()

    def _split_attachment_urls(raw_value: Any) -> list[str]:
        if raw_value is None:
            return []
        parts = [p.strip() for p in str(raw_value).split(",")]
        return [p for p in parts if p]

    def _merge_attachment_urls(existing_raw: str, new_urls: list[str]) -> str:
        merged: list[str] = []
        seen: set[str] = set()
        for url in _split_attachment_urls(existing_raw) + new_urls:
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(url)
        return ",".join(merged)

    for key in form_data.keys():
        if key == "_action" or key.startswith("__batch_") or key.startswith("__existing_attachment_urls_"):
            continue

        values = form_data.getlist(key)
        if not values:
            continue

        upload_values = [v for v in values if isinstance(v, UploadFile) or hasattr(v, "filename")]
        if upload_values:
            uploaded_urls: list[str] = []
            for val in upload_values:
                if not getattr(val, "filename", None):
                    continue
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
                safe_filename = f"{timestamp}_{val.filename}"
                file_path = upload_base_dir / safe_filename

                contents = await val.read()
                with open(file_path, "wb") as f:
                    f.write(contents)

                base_url = str(request.base_url).rstrip("/")
                uploaded_urls.append(f"{base_url}/static/uploads/{attachment_folder_name}/{safe_filename}")

            if uploaded_urls:
                payload[key] = _merge_attachment_urls(existing_attachment_map.get(key, ""), uploaded_urls)
            continue

        val = values[-1]
        if isinstance(val, str) and val.strip() == "":
            payload[key] = None
        else:
            payload[key] = val

    if raw_batch_payload:
        try:
            batch_rows = json.loads(str(raw_batch_payload))
        except (TypeError, ValueError):
            batch_rows = []

        if isinstance(batch_rows, list):
            for item in batch_rows:
                if not isinstance(item, dict):
                    continue
                
                batch_key = str(item.get("batch_key") or "").strip()
                child_row_id = str(item.get("row_id") or "").strip()
                packed_val = "1" if bool(item.get("packed")) else "0"
                remarks_val = str(item.get("remarks") or "").strip()
                
                if child_row_id and child_row_id.lower() != "none" and child_row_id != "":
                    child_payload = {
                        "action": "update",
                        "table": "Z_SOPickNpackBatchDetails",
                        "data": {
                            "S1_Submission": packed_val,
                            "S1_Remarks": remarks_val
                        },
                        "where": {
                            "RowID": child_row_id
                        }
                    }
                    try:
                        await api_client.post(
                            "update_row",
                            json_body=child_payload
                        )
                        print(f"Appended successfully to RowID {child_row_id}")
                    except Exception as e:
                        print(f"Failed to update batch row {child_row_id}: {e}")

    # Stage 1: the picker is saved through the dedicated S button endpoint.
    # The main form still persists the already-saved comma-separated value.
    if str(stage_no).strip().upper() == "S1":
        raw_picker_value = payload.get("S1_Picker")
        if raw_picker_value in (None, ""):
            raw_picker_value = _pick_value_by_suffix(stage_context.get("row") or {}, ("s1picker",), "S1")
        picker_values = _parse_picker_list(raw_picker_value)
        if len(picker_values) > 5:
            raise ValueError("Stage 1 allows a maximum of 5 pickers.")
        payload["S1_Picker"] = ", ".join(picker_values) if picker_values else None
        if action in {"submit", "append"} and not picker_values:
            raise ValueError("Stage 1 cannot be submitted until at least one picker is saved.")

    if str(stage_no).strip().upper() == "S1" and action in {"submit", "append"}:
        # Every Sales Order line represented by this Stage 1 must have its own
        # S1_PickIDWeightkgs saved in Z_SalesOrder before the stage is submitted.
        current_doc = _to_text(_pick_value_by_suffix(stage_context.get("row") or {}, ("idocnum", "docnum", "salesorderno")))
        related_rows_for_weight = []
        current_create_ts = _to_text(_pick_value_by_suffix(stage_context.get("row") or {}, ("icreatets", "createts", "createtimestamp", "createdts")))
        if current_doc:
            try:
                fetched_weight_rows = await table_api.get_table("Z_SalesOrder", filters={"I_DocNum": current_doc})
            except Exception:
                fetched_weight_rows = []
            if current_create_ts:
                related_rows_for_weight = [
                    sales_row for sales_row in fetched_weight_rows
                    if _to_text(_pick_value_by_suffix(sales_row, ("icreatets", "createts", "createtimestamp", "createdts"))) == current_create_ts
                ]
            else:
                related_rows_for_weight = fetched_weight_rows
        if not related_rows_for_weight:
            related_rows_for_weight = [stage_context.get("row") or {}]
        missing_weight_lines = []
        for sales_row in related_rows_for_weight:
            line_no = _to_text(_pick_value_by_suffix(sales_row, ("ilinenum", "linenum")))
            weight_value = _pick_value_by_suffix(sales_row, ("s1pickidweightkgs",), "S1")
            if line_no != "" and (weight_value in (None, "") or str(weight_value).strip().lower() in {"none", "null"}):
                missing_weight_lines.append(line_no)
        if missing_weight_lines:
            raise ValueError("Save Weight for line(s) before submitting Stage 1: " + ", ".join(missing_weight_lines))

    employee_code = _get_authenticated_employee_code(request)
    _inject_update_audit_fields(payload, stage_context.get("row") or {}, employee_code)

    # ------------------------------------------------------------------
    # STAGE 2: persist the six user-entered fields directly against the
    # physical Z_SalesOrder RowID.  Do not rely only on the generic stage
    # field mapper here: these are known business columns and must land in
    # the exact SQL columns.  This also makes the saved values immediately
    # available to the downstream S2 processing/SP.
    # ------------------------------------------------------------------
    if str(stage_no).strip().upper() == "S2" and payload:
        s2_fields = (
            "S2_VehicleOrigin",
            "S2_VehicleType",
            "S2_AssignedDriver",
            "S2_DriverContact",
            "S2_VehicleNo",
            "S2_DispatchDateTime",
            "S2_LoadingImage1",
            "S2_Remarks",
        )
        s2_payload = {key: payload.get(key) for key in s2_fields if key in payload}

        if "S2_DriverContact" in s2_payload and _to_text(s2_payload.get("S2_DriverContact")):
            mobile_digits = re.sub(r"\D", "", _to_text(s2_payload.get("S2_DriverContact")))
            if len(mobile_digits) != 10:
                raise ValueError("Driver Contact must contain exactly 10 digits.")
            s2_payload["S2_DriverContact"] = mobile_digits

        # SQL Server column S2_DispatchDateTime is datetime. HTML
        # datetime-local submits ISO text such as 2026-09-15T10:30, which
        # the generic API update path was passing through as a string and
        # causing SQL conversion error 241. Normalize before the update.
        if "S2_DispatchDateTime" in s2_payload:
            s2_payload["S2_DispatchDateTime"] = _normalize_sql_datetime(
                s2_payload.get("S2_DispatchDateTime")
            )

        if s2_payload:
            # Stage 2 belongs to the complete PickID group.  Persist the exact
            # same S2 payload to every Z_SalesOrder row carrying this PickID.
            s2_pick_rows = await _resolve_salesorder_row_ids_by_pick_id(stage_context, task_id)
            if not s2_pick_rows:
                raise RuntimeError("Stage 2 could not resolve any rows for the current PickID.")

            for physical_row_id in s2_pick_rows:
                update_body = {
                    "action": "update",
                    "table": fms_info.get("table_name"),
                    "data": s2_payload,
                    "where": {"RowID": physical_row_id},
                }
                update_result = await api_client.post(
                    "update_row",
                    query_params={
                        "action": "update",
                        "table": fms_info.get("table_name"),
                    },
                    json_body=update_body,
                )
                print(
                    f"[S2 Direct Save][PickID] table={fms_info.get('table_name')} "
                    f"PickID={_to_text(_pick_value_by_suffix(stage_context.get('row') or {}, ('s1pickid','pickid'), 'S1'))} "
                    f"RowID={physical_row_id} fields={sorted(s2_payload.keys())} result={update_result}"
                )

                if isinstance(update_result, dict) and str(update_result.get("status", "")).lower() == "error":
                    error_detail = update_result.get("error") or update_result
                    raise RuntimeError(f"Stage 2 database update failed for RowID {physical_row_id}: {error_detail}")

                refreshed_row = await table_api.get_row(fms_info.get("table_name"), physical_row_id)
                if isinstance(refreshed_row, dict):
                    mismatches = []
                    for key, expected in s2_payload.items():
                        actual = _row_value_ci(refreshed_row, key)
                        if expected in (None, ""):
                            continue
                        if not _same_saved_value(expected, actual, key):
                            mismatches.append(f"{key}: expected={expected!r}, actual={actual!r}")
                    if mismatches:
                        raise RuntimeError(
                            f"Stage 2 values were not persisted to RowID {physical_row_id}: "
                            + "; ".join(mismatches)
                        )

            # Keep the form's primary row fresh for the downstream stage/SP logic.
            current_physical_id = s2_pick_rows[0]
            refreshed_row = await table_api.get_row(fms_info.get("table_name"), current_physical_id)
            if isinstance(refreshed_row, dict):
                stage_context["row"] = refreshed_row

        # Keep the generic stage save for non-S2 fields/audit fields where
        # applicable, but never let it overwrite the explicit S2 mapping.
        generic_payload = {k: v for k, v in payload.items() if k not in s2_fields}
        if generic_payload:
            await stage_service.save_stage(task_id, stage_no, generic_payload)

        # Refresh the complete stage context after the direct DB update.
        stage_context = await stage_service.get_stage_context(task_id, stage_no)
    elif str(stage_no).strip().upper() == "S3" and payload:
        s3_fields = (
            "S3_DelDocNo",
            "S3_DelLineNo",
            "S3_PackingSlip",
        )
        s3_payload = {key: payload.get(key) for key in s3_fields if key in payload}

        # Stage 3 delivery document/line values are row-specific. Save the
        # entered S3 values to the current physical Z_SalesOrder RowID only.
        if s3_payload:
            update_body = {
                "action": "update",
                "table": fms_info.get("table_name"),
                "data": s3_payload,
                "where": {"RowID": str(task_id)},
            }
            update_result = await api_client.post(
                "update_row",
                query_params={
                    "action": "update",
                    "table": fms_info.get("table_name"),
                },
                json_body=update_body,
            )
            print(
                f"[S3 Direct Save] table={fms_info.get('table_name')} "
                f"RowID={task_id} fields={sorted(s3_payload.keys())} result={update_result}"
            )
            if isinstance(update_result, dict) and str(update_result.get("status", "")).lower() == "error":
                error_detail = update_result.get("error") or update_result
                raise RuntimeError(f"Stage 3 database update failed for RowID {task_id}: {error_detail}")

            refreshed_row = await table_api.get_row(fms_info.get("table_name"), task_id)
            if isinstance(refreshed_row, dict):
                stage_context["row"] = refreshed_row

        generic_payload = {k: v for k, v in payload.items() if k not in s3_fields}
        if generic_payload:
            await stage_service.save_stage(task_id, stage_no, generic_payload)
        stage_context = await stage_service.get_stage_context(task_id, stage_no)

    elif payload:
        await stage_service.save_stage(task_id, stage_no, payload)

    if str(stage_no).strip().upper() == "S2":
        related_row_ids = [rid for rid in await _resolve_salesorder_row_ids_by_pick_id(stage_context, task_id) if str(rid) != str(task_id)]
    else:
        related_row_ids = await _resolve_related_salesorder_row_ids(stage_context, task_id)
    affected_row_ids = [task_id, *related_row_ids]
    if related_row_ids and payload:
        for related_row_id in related_row_ids:
            try:
                await table_api.update_row(stage_context["fms"]["table_name"], related_row_id, payload)
            except Exception as sync_exc:
                print(f"Failed to sync payload to related row {related_row_id}: {sync_exc}")

    if action in {"submit", "append"} and str(stage_no).strip().upper() == "S3":
        current_row_for_validation = stage_context.get("row") or {}

        def _s3_submitted_or_existing(field_name: str, *aliases: str) -> str:
            raw = payload.get(field_name)
            if raw is None or str(raw).strip() == "":
                raw = _row_value_ci(current_row_for_validation, field_name)
            if raw is None or str(raw).strip() == "":
                for alias in aliases:
                    raw = payload.get(alias)
                    if raw is None or str(raw).strip() == "":
                        raw = _row_value_ci(current_row_for_validation, alias)
                    if raw is not None and str(raw).strip() != "":
                        break
            return "" if raw is None else str(raw).strip()

        required_values = {
            "S3_DelDocNo": _s3_submitted_or_existing("S3_DelDocNo", "S3_DeliveryDocNo", "S3_DelDoc_No"),
            "S3_DelLineNo": _s3_submitted_or_existing("S3_DelLineNo", "S3_DeliveryLineNo", "S3_DelLine_No"),
            "S3_PackingSlip": _s3_submitted_or_existing("S3_PackingSlip", "S3_Packing_Slip"),
        }
        missing_fields = [name for name, value in required_values.items() if not value]
        if missing_fields:
            raise ValueError(
                "Stage 3 cannot be submitted until all required fields are filled: "
                + ", ".join(missing_fields)
            )

    if action in {"submit", "append"} and str(stage_no).strip().upper() == "S2":
        # Hard server-side gate: S2 can only be submitted once all required user
        # inputs exist and at least one loading-image attachment is present.
        current_row_for_validation = stage_context.get("row") or {}

        def _submitted_or_existing(field_name: str, *aliases: str) -> str:
            raw = payload.get(field_name)
            if raw is None or str(raw).strip() == "":
                raw = _row_value_ci(current_row_for_validation, field_name)
            if raw is None or str(raw).strip() == "":
                for alias in aliases:
                    raw = payload.get(alias)
                    if raw is None or str(raw).strip() == "":
                        raw = _row_value_ci(current_row_for_validation, alias)
                    if raw is not None and str(raw).strip() != "":
                        break
            return "" if raw is None else str(raw).strip()

        required_values = {
            "S2_VehicleOrigin": _submitted_or_existing("S2_VehicleOrigin", "S2_Vehicle_Origin"),
            "S2_VehicleType": _submitted_or_existing("S2_VehicleType", "S2_Vehicle_Type"),
            "S2_AssignedDriver": _submitted_or_existing("S2_AssignedDriver", "S2_DriverName", "DriverName"),
            "S2_DriverContact": _submitted_or_existing("S2_DriverContact", "S2_DriverNumber", "DriverNumber", "DriverMobileNo"),
            "S2_VehicleNo": _submitted_or_existing("S2_VehicleNo", "S2_Vehicle_No"),
            "S2_DispatchDateTime": _submitted_or_existing("S2_DispatchDateTime", "S2_Dispatch_Date_Time"),
            "S2_Remarks": _submitted_or_existing("S2_Remarks"),
            "S2_LoadingImage1": _submitted_or_existing("S2_LoadingImage1", "S2_Loading_Image1"),
        }

        mobile_digits = re.sub(r"\D", "", required_values.get("S2_DriverContact", ""))
        if mobile_digits and len(mobile_digits) != 10:
            raise ValueError("Driver Contact must contain exactly 10 digits.")

        missing_fields = [name for name, value in required_values.items() if not value]
        if missing_fields:
            raise ValueError(
                "Stage 2 cannot be submitted until all required fields are filled: "
                + ", ".join(missing_fields)
            )

    # Track whether SP_FMS_ALTimeStampStatus completed for this submission.
    # SP_FMS_Pack2Dispatch_RowCycleStatus will run only after every downstream
    # stage-specific/linked SP for this submission has completed.
    altimestamp_sp_ran = False

    if action in {"submit", "append"}:
        stage_row = stage_context.get("row") or {}
        stage_info = stage_context.get("stage") or {}
        fms_info = stage_context.get("fms") or {}

        # ================================================================
        # EXACT INPUTS REQUIRED BY SP_FMS_ALTimeStampStatus
        #
        # @PLTimeStamp
        # @ALTimeStamp
        # @DoerCode
        # @RowID
        # @AppID
        # @StageNo
        # @LeadTimeMins
        # @KPIApplicable
        # @ALHelper
        #
        # @DelayMins and @TaskStatus are OUTPUT values handled by SQL.
        # ================================================================

        app_id_for_sp = _to_text(
            fms_info.get("app_id")
            or fms_info.get("APP_ID")
            or app_id
        )

        stage_no_for_sp = _to_text(
            stage_info.get("STAGE_NO")
            or stage_no
        )

        if not app_id_for_sp:
            raise ValueError(
                f"SP_FMS_ALTimeStampStatus: AppID could not be resolved "
                f"for RowID={task_id}, Stage={stage_no_for_sp}"
            )

        if not stage_no_for_sp:
            raise ValueError(
                f"SP_FMS_ALTimeStampStatus: StageNo could not be resolved "
                f"for RowID={task_id}"
            )

        # ------------------------------------------------------------
        # PL TIMESTAMP
        # ------------------------------------------------------------
        planned_col = (
            str(stage_info.get("PLANNED_COLUMN") or "").strip()
            or _find_stage_field_name(
                stage_context,
                stage_no_for_sp,
                "pltimestamp",
                "pltime",
                "plbasets",
                "plannedtimestamp",
                "planneddatetime",
                "tspltimestamp",
            )
        )

        pl_timestamp = (
            payload.get(planned_col)
            if planned_col
            else None
        )

        if pl_timestamp is None and planned_col:
            pl_timestamp = _row_value_ci(
                stage_row,
                planned_col
            )

        # ------------------------------------------------------------
        # AL TIMESTAMP
        # Read/generate this value for the SP only.
        # The form does NOT write the AL timestamp column.
        # ------------------------------------------------------------
        actual_col = (
            str(stage_info.get("ACTUAL_COLUMN") or "").strip()
            or _find_stage_field_name(
                stage_context,
                stage_no_for_sp,
                "altimestamp",
                "actualtimestamp",
                "actualdatetime",
                "altime",
            )
        )

        al_timestamp = (
            payload.get(actual_col)
            if actual_col
            else None
        )

        if not al_timestamp and actual_col:
            al_timestamp = _row_value_ci(
                stage_row,
                actual_col
            )

        if not al_timestamp:
            al_timestamp = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        # ------------------------------------------------------------
        # AL HELPER
        # Prepare this value for the SP only.
        # The form does NOT write the ALHelper column.
        # ------------------------------------------------------------
        alhelper_col = _find_stage_field_name(
            stage_context,
            stage_no_for_sp,
            "alhelper",
        )

        al_helper = (
            payload.get(alhelper_col)
            if alhelper_col
            else None
        )

        if not al_helper and alhelper_col:
            al_helper = _row_value_ci(
                stage_row,
                alhelper_col
            )

        al_helper = _to_text(al_helper) or "FMS"

        # ------------------------------------------------------------
        # DOER CODE
        # ------------------------------------------------------------
        doer_emp_code_col = _find_stage_field_name(
            stage_context,
            stage_no_for_sp,
            "doerempcode",
            "doerec",
            "employeecode",
        )

        doer_name_col = _find_stage_field_name(
            stage_context,
            stage_no_for_sp,
            "doer",
        )

        doer_code = None

        if doer_emp_code_col:
            doer_code = (
                payload.get(doer_emp_code_col)
                or _row_value_ci(
                    stage_row,
                    doer_emp_code_col
                )
            )

        # FMS_Config may already contain DOER_EMP_CODE.
        if not doer_code:
            doer_code = stage_info.get("DOER_EMP_CODE")

        # If only S1_Doer / S2_Doer exists, resolve Name -> EmployeeCode.
        doer_name = None

        if doer_name_col:
            doer_name = (
                payload.get(doer_name_col)
                or _row_value_ci(
                    stage_row,
                    doer_name_col
                )
            )

        if not doer_name:
            doer_name = stage_info.get("DOER")

        if not doer_code and doer_name:
            doer_name_text = _to_text(doer_name)

            if doer_name_text:
                try:
                    employee_rows = await table_api.get_table(
                        "VM_EmployeeMaster"
                    )

                    for employee_row in employee_rows or []:
                        employee_name_value = (
                            employee_row.get("EmployeeName")
                            or employee_row.get("EMPLOYEENAME")
                            or employee_row.get("Employee_Name")
                            or employee_row.get("Name")
                        )

                        if (
                            _normalize_key(employee_name_value)
                            == _normalize_key(doer_name_text)
                        ):
                            doer_code = (
                                employee_row.get("EmployeeCode")
                                or employee_row.get("EMPLOYEECODE")
                                or employee_row.get("employee_code")
                            )
                            break

                    print(
                        "[SP_FMS_ALTimeStampStatus] "
                        f"Resolved Doer Name '{doer_name_text}' "
                        f"-> EmployeeCode '{doer_code}'"
                    )

                except Exception as exc:
                    print(
                        "[SP_FMS_ALTimeStampStatus] "
                        f"Failed resolving Doer Name "
                        f"'{doer_name_text}': {exc}"
                    )

        doer_code = _to_text(doer_code)

        if not doer_code:
            raise ValueError(
                "SP_FMS_ALTimeStampStatus: "
                f"Could not resolve DoerCode for "
                f"RowID='{task_id}', "
                f"AppID='{app_id_for_sp}', "
                f"Stage='{stage_no_for_sp}'"
            )

        # ------------------------------------------------------------
        # LEAD TIME
        # ------------------------------------------------------------
        lead_time_col = _find_stage_field_name(
            stage_context,
            stage_no_for_sp,
            "leadtimemins",
            "leadtime",
        )

        lead_time_mins = None

        if lead_time_col:
            lead_time_mins = (
                payload.get(lead_time_col)
                or _row_value_ci(
                    stage_row,
                    lead_time_col
                )
            )

        try:
            lead_time_mins = (
                int(str(lead_time_mins).strip())
                if lead_time_mins not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            lead_time_mins = None

        # ------------------------------------------------------------
        # KPI APPLICABLE
        # ------------------------------------------------------------
        kpi_flag_col = _find_stage_field_name(
            stage_context,
            stage_no_for_sp,
            "kpiapplicable",
            "kpiapp",
        )

        kpi_applicable_raw = None

        if kpi_flag_col:
            kpi_applicable_raw = (
                payload.get(kpi_flag_col)
                or _row_value_ci(
                    stage_row,
                    kpi_flag_col
                )
            )

        kpi_applicable = (
            "Y"
            if _to_text(kpi_applicable_raw).upper()
            in {"Y", "YES", "1", "TRUE"}
            else "N"
        )

        # ------------------------------------------------------------
        # EXACT SP PAYLOAD
        # We run this once for every row in the current form-batch, not just
        # the current task. That ensures all rows in the same submission are
        # timestamped together.
        # ------------------------------------------------------------
        # Use the actual physical primary-key value for the current row.
        # The dashboard/task id is not guaranteed to be identical after table
        # recreation or when an API gateway introduces a task wrapper id.
        physical_current_row_id = stage_service._context_row_id(stage_context, task_id)
        affected_row_ids = [str(physical_current_row_id), *[str(r) for r in related_row_ids]]
        unique_row_ids = []
        seen_row_ids = set()
        for row_id_for_batch in affected_row_ids:
            if row_id_for_batch and row_id_for_batch not in seen_row_ids:
                seen_row_ids.add(row_id_for_batch)
                unique_row_ids.append(row_id_for_batch)

        print(
            "[SP_FMS_ALTimeStampStatus] Batch row IDs: "
            f"{unique_row_ids}"
        )

        for row_id_for_sp in unique_row_ids:
            proc_payload = {
                "PLTimeStamp": _to_text(pl_timestamp) or None,
                "ALTimeStamp": _to_text(al_timestamp) or None,
                "DoerCode": doer_code,
                "RowID": int(row_id_for_sp),
                "AppID": app_id_for_sp,
                "StageNo": stage_no_for_sp,
                "LeadTimeMins": lead_time_mins,
                "KPIApplicable": kpi_applicable,
                "ALHelper": al_helper,
            }

            print(
                "[SP_FMS_ALTimeStampStatus] Prepared execution: "
                f"PL='{proc_payload['PLTimeStamp']}', "
                f"AL='{proc_payload['ALTimeStamp']}', "
                f"DoerCode='{proc_payload['DoerCode']}', "
                f"RowID='{proc_payload['RowID']}', "
                f"AppID='{proc_payload['AppID']}', "
                f"StageNo='{proc_payload['StageNo']}', "
                f"LeadTimeMins='{proc_payload['LeadTimeMins']}', "
                f"KPIApplicable='{proc_payload['KPIApplicable']}', "
                f"ALHelper='{proc_payload['ALHelper']}'"
            )

            print("\n========== FORM -> SP ==========")
            print("PLTimeStamp  :", repr(proc_payload["PLTimeStamp"]))
            print("ALTimeStamp  :", repr(proc_payload["ALTimeStamp"]))
            print("DoerCode     :", repr(proc_payload["DoerCode"]))
            print("RowID        :", repr(proc_payload["RowID"]))
            print("AppID        :", repr(proc_payload["AppID"]))
            print("StageNo      :", repr(proc_payload["StageNo"]))
            print("LeadTimeMins :", repr(proc_payload["LeadTimeMins"]))
            print("KPIApplicable:", repr(proc_payload["KPIApplicable"]))
            print("ALHelper     :", repr(proc_payload["ALHelper"]))
            print("================================\n")

            print("[SP_FMS_ALTimeStampStatus] FINAL PAYLOAD:")
            print(json.dumps(proc_payload, default=str, ensure_ascii=False, indent=2))
            print("[SP_FMS_ALTimeStampStatus] END FINAL PAYLOAD\n")

            # ------------------------------------------------------------
            # RUN SP
            # SQL handles @DelayMins OUTPUT and @TaskStatus OUTPUT.
            # SQL also persists S1_/S2_ fields itself using @StageNo.
            # ------------------------------------------------------------
            try:
                sp_result = await stage_service.run_named_stage_procedure(
                    task_id,
                    stage_no_for_sp,
                    "SP_FMS_ALTimeStampStatus",
                    proc_payload,
                )

                print(
                    "[SP_FMS_ALTimeStampStatus] Completed: "
                    f"Stage='{stage_no_for_sp}', "
                    f"RowID='{row_id_for_sp}', "
                    f"Result={sp_result}"
                )

            except Exception as proc_exc:
                print(
                    "[SP_FMS_ALTimeStampStatus] FAILED: "
                    f"Stage='{stage_no_for_sp}', "
                    f"RowID='{row_id_for_sp}', "
                    f"Error={proc_exc}"
                )
                raise

        # All SP_FMS_ALTimeStampStatus executions for the current submission
        # completed successfully. Downstream stage SPs can now run, and the
        # RowCycleStatus SP will be deferred until ALL of them are finished.
        if unique_row_ids:
            altimestamp_sp_ran = True

        # Pick/Pack custom Stage-1 detection. This gate intentionally limits
        # the explicit SP_FMS_Pack2Dispatch_S3 chain to the custom Stage-1
        # form handled by stage1_custom_service.
        current_stage_no = str(stage_no or "").strip().upper()
        is_pick_pack_custom_s1 = False
        is_pick_pack_custom_s3 = False
        if current_stage_no == "S1":
            try:
                custom_stage1_check = await stage1_custom_service.build_stage1_custom_context(stage_context)
                is_pick_pack_custom_s1 = bool(custom_stage1_check.get("enabled"))
            except Exception as custom_check_exc:
                print(
                    "[S1 CUSTOM CHAIN] Could not resolve Pick/Pack custom form: "
                    f"{custom_check_exc}"
                )
        if current_stage_no == "S3":
            try:
                fms_info = stage_context.get("fms") or {}
                table_name = str(fms_info.get("table_name") or "").strip()
                fms_name = str(
                    fms_info.get("fms_name")
                    or fms_info.get("FMS_Name")
                    or fms_info.get("name")
                    or ""
                ).strip().casefold()
                detected_s3_template = _detect_stage_custom_template(table_name, "S3")
                is_pick_pack_custom_s3 = (
                    table_name.casefold() == "z_salesorder"
                    and (
                        _normalize_key(fms_name) == "packtodispatchdetails"
                        or not _normalize_key(fms_name)
                        or detected_s3_template == "custom_forms/stage_S3.html"
                    )
                )
                print(
                    "[S3 CUSTOM CHAIN] detection: "
                    f"fms='{fms_name}', table='{table_name}', template='{detected_s3_template}', "
                    f"enabled={is_pick_pack_custom_s3}"
                )
            except Exception as custom_s3_check_exc:
                print(f"[S3 CUSTOM CHAIN] Could not resolve custom form: {custom_s3_check_exc}")
                is_pick_pack_custom_s3 = False
        print(
            "[CUSTOM CHAIN] "
            f"stage='{current_stage_no}' pick_pack_custom_s1={is_pick_pack_custom_s1} "
            f"pick_pack_custom_s3={is_pick_pack_custom_s3}"
        )
        # ------------------------------------------------------------
        # WAIT AFTER THE S1 SP FINISHES, THEN FIRE THE LINKED SP.
        # This ensures the row-level S1 fields (including ALTimeStamp) are
        # written before the downstream linked procedure reads them.
        # ------------------------------------------------------------
        print(
            "[SP_FMS_ALTimeStampStatus] "
            "Waiting 2 seconds after S1 SP execution before linked SP..."
        )
        await asyncio.sleep(2)

        # ====================================================================
        # MANDATORY PACK-TO-DISPATCH S3 -> S4 CHAIN
        #
        # S3 submit must execute the downstream S4 procedure immediately
        # after SP_FMS_ALTimeStampStatus.  Keep this path isolated from the
        # generic linked-SP resolver so a later metadata/config lookup cannot
        # turn a successfully saved S3 submission into an HTTP 500.
        # ====================================================================
        current_stage_no = str(stage_no or "").strip().upper()
        s3_s4_chain_executed = False
        if current_stage_no == "S3":
            chain_fms = stage_context.get("fms") or {}
            chain_table = str(
                chain_fms.get("table_name")
                or chain_fms.get("TABLE_NAME")
                or ""
            ).strip()
            chain_fms_name = str(
                chain_fms.get("fms_name")
                or chain_fms.get("FMS_Name")
                or chain_fms.get("name")
                or ""
            ).strip()
            is_pack_dispatch_s3 = chain_table.casefold() == "z_salesorder"
            print(
                "[S3->S4 CHAIN] GATE: "
                f"task={task_id!r} table={chain_table!r} fms={chain_fms_name!r} "
                f"pack_dispatch_table={is_pack_dispatch_s3!r}"
            )

            if is_pack_dispatch_s3:
                try:
                    physical_row_id = _to_text(
                        _row_value_ci(
                            stage_context.get("row") or {},
                            stage_context.get("primary_key") or "RowID",
                        )
                        or _row_value_ci(stage_context.get("row") or {}, "RowID")
                        or task_id
                    )
                    latest_row = await table_api.get_row(chain_table, physical_row_id)
                    if not isinstance(latest_row, dict):
                        latest_row = stage_context.get("row") or {}

                    i_doc_entry = _pick_value_by_suffix(
                        latest_row, ("idocentry", "docentry", "salesorderentry")
                    )
                    i_doc_num = _pick_value_by_suffix(
                        latest_row, ("idocnum", "docnum", "salesorderno")
                    )
                    create_ts = _pick_value_by_suffix(
                        latest_row, ("icreatets", "createts", "createtimestamp", "createdts")
                    )
                    s3_del_doc = _row_value_ci(latest_row, "S3_DelDocNo")
                    s3_del_line = _row_value_ci(latest_row, "S3_DelLineNo")

                    print(
                        "[S3->S4 CHAIN] SOURCE DATA: "
                        + json.dumps(
                            {
                                "RowID": physical_row_id,
                                "I_DocEntry": i_doc_entry,
                                "I_DocNum": i_doc_num,
                                "I_CreateTS": create_ts,
                                "S3_DelDocNo": s3_del_doc,
                                "S3_DelLineNo": s3_del_line,
                                "S3_ALTimeStamp": _row_value_ci(latest_row, "S3_ALTimeStamp"),
                                "I_DelInvClear": _row_value_ci(latest_row, "I_DelInvClear"),
                                "I_RowCycleStatus": _row_value_ci(latest_row, "I_RowCycleStatus"),
                                "I_DelShippingType": _row_value_ci(latest_row, "I_DelShippingType"),
                                "I_Location": _row_value_ci(latest_row, "I_Location"),
                                "S4_PLTimeStamp": _row_value_ci(latest_row, "S4_PLTimeStamp"),
                            },
                            default=str, ensure_ascii=False, sort_keys=True,
                        )
                    )

                    # EXACT SQL SIGNATURE:
                    #   @DocEntrySO INT, @TriggerSource NVARCHAR(20) = 'App'
                    s4_proc_data = {
                        "DocEntrySO": i_doc_entry,
                        "TriggerSource": "App",
                    }
                    s4_request_payload = {
                        "table": chain_table,
                        "task_id": task_id,
                        "stage_no": "S4",
                        "procedure": "SP_FMS_Pack2Dispatch_S4",
                        "linked_stored_procedure": "SP_FMS_Pack2Dispatch_S4",
                        "data": s4_proc_data,
                    }

                    print(
                        "[S3->S4 CHAIN] SP_FMS_Pack2Dispatch_S4 SQL SIGNATURE: "
                        "@DocEntrySO INT, @TriggerSource NVARCHAR(20) = 'App'"
                    )
                    print(
                        "[S3->S4 CHAIN] SP_FMS_Pack2Dispatch_S4 EXACT INPUTS: "
                        + json.dumps(s4_proc_data, default=str, ensure_ascii=False, sort_keys=True)
                    )
                    print(
                        "[S3->S4 CHAIN] SP_FMS_Pack2Dispatch_S4 RUNNING: "
                        f"task={task_id!r}, physical_row={physical_row_id!r}, DocEntrySO={i_doc_entry!r}"
                    )

                    if i_doc_entry is None or not str(i_doc_entry).strip():
                        raise RuntimeError(
                            "SP_FMS_Pack2Dispatch_S4 NOT RUN: DocEntrySO could not be resolved."
                        )

                    try:
                        s4_result = await task_api.save_task(s4_request_payload)
                    except Exception as s4_api_exc:
                        print(
                            "[S3->S4 CHAIN] SP_FMS_Pack2Dispatch_S4 FAILED: "
                            f"task={task_id!r}, DocEntrySO={i_doc_entry!r}, error={s4_api_exc!r}"
                        )
                        raise

                    print(
                        "[S3->S4 CHAIN] SP_FMS_Pack2Dispatch_S4 RESPONSE: "
                        f"{s4_result!r}"
                    )

                    if isinstance(s4_result, dict):
                        status_text = str(s4_result.get("status") or "").strip().casefold()
                        if status_text in {"error", "failed", "failure"} or s4_result.get("success") is False:
                            raise RuntimeError(
                                "SP_FMS_Pack2Dispatch_S4 returned an error: "
                                f"{s4_result.get('message') or s4_result}"
                            )

                    verify_row = await table_api.get_row(chain_table, physical_row_id)
                    s4_verify = {
                        "S4_PLHelper": _row_value_ci(verify_row or {}, "S4_PLHelper"),
                        "S4_PLBaseTS": _row_value_ci(verify_row or {}, "S4_PLBaseTS"),
                        "S4_LeadTimeMins": _row_value_ci(verify_row or {}, "S4_LeadTimeMins"),
                        "S4_PLTimeStamp": _row_value_ci(verify_row or {}, "S4_PLTimeStamp"),
                        "S4_Doer": _row_value_ci(verify_row or {}, "S4_Doer"),
                        "S4_TaskStatus": _row_value_ci(verify_row or {}, "S4_TaskStatus"),
                    }
                    print(
                        "[S3->S4 CHAIN] POST-SP VERIFY: "
                        + json.dumps(s4_verify, default=str, ensure_ascii=False, sort_keys=True)
                    )

                    # HTTP 200 only proves that the API accepted the call.
                    # The stored procedure intentionally creates no S4 task when
                    # its SQL eligibility predicate is not satisfied (for example
                    # I_DelInvClear <> 'Clear').  Do not turn that valid business
                    # outcome into an HTTP 500 after S3 has already been saved.
                    s3_s4_chain_executed = True
                    if s4_verify["S4_PLTimeStamp"] in (None, ""):
                        print(
                            "[S3->S4 CHAIN] NO S4 TASK CREATED: "
                            "SP_FMS_Pack2Dispatch_S4 executed but S4_PLTimeStamp remains NULL. "
                            "Check the logged source data against the SP eligibility conditions."
                        )
                    else:
                        print(
                            "[S3->S4 CHAIN] SUCCESS: "
                            f"S4_PLTimeStamp={s4_verify['S4_PLTimeStamp']!r} task={task_id!r}"
                        )
                except Exception as s4_exc:
                    print(
                        "[S3->S4 CHAIN] FAILED: "
                        f"task={task_id!r} error={s4_exc!r}"
                    )
                    raise
            else:
                print(
                    "[S3->S4 CHAIN] NOT RUN: "
                    "current form is not Z_SalesOrder."
                )

            # S3 has no additional generic linked-SP work.  Once the S3 -> S4
            # chain has completed, do not redirect the iframe back through the
            # S3 GET handler.  That handler performs additional upstream reads
            # and a transient API failure there must not turn a successfully
            # saved submission into a 500 page.
            if s3_s4_chain_executed:
                print(
                    "[S3 SUBMIT] COMPLETE: S3 saved and S3->S4 downstream chain completed; "
                    "returning success response without reloading the form route."
                )
                success_html = """<!doctype html>
<html>
<head><meta charset=\"utf-8\"><title>S3 Saved</title></head>
<body>
<script>
(function () {
  try {
    if (window.parent && window.parent !== window && typeof window.parent.closeStageModal === 'function') {
      window.parent.closeStageModal();
      return;
    }
  } catch (e) {}
  try {
    if (typeof window.refreshDashboardLive === 'function') {
      window.refreshDashboardLive('stage-saved');
    }
  } catch (e) {}
  window.location.replace('/dashboard');
})();
</script>
<div style=\"font-family:Arial,sans-serif;padding:40px;text-align:center;\">S3 saved successfully. Stage 4 has been processed.</div>
</body>
</html>"""
                return HTMLResponse(content=success_html, status_code=200)

        try:
            task_context = await task_service.get_task_context(task_id)
            stage_configs = task_context.get("stage_configs") or []
            current_stage_no = str(stage_no or "").strip().upper()

            # ------------------------------------------------------------
            # STAGE 2 SPECIAL FLOW
            # ------------------------------------------------------------
            # For S2, always execute the current-stage Linked Stored
            # Procedure (currently SP_FMS_Pack2Dispatch_S2) when configured,
            # but NEVER use its success/failure/result as a gate for S3.
            # SP_FMS_Pack2Dispatch_S3 must run after the S2 attempt so S3
            # planning values can be populated independently.
            if current_stage_no == "S2":
                current_stage = next(
                    (c for c in stage_configs if str((c or {}).get("STAGE_NO") or "").strip().upper() == "S2"),
                    None,
                ) or (stage_context.get("stage") or {})

                linked_proc_name = str(
                    (current_stage or {}).get("Linked_Stored_Procedure")
                    or (current_stage or {}).get("LINKED_STORED_PROCEDURE")
                    or (current_stage or {}).get("linked_stored_procedure")
                    or ""
                ).strip()

                if linked_proc_name and linked_proc_name.lower() != "sp_fms_altimestampstatus":
                    print(
                        "[S2 Linked SP] Running current-stage procedure "
                        f"stage='S2' procedure='{linked_proc_name}'"
                    )
                    # IMPORTANT: S3 is NOT gated by S2. If the S2 linked
                    # procedure fails or its business conditions are not met,
                    # log the failure and continue to S3.
                    try:
                        s2_linked_result = await stage_service.run_linked_stage_procedure(
                            task_id, "S2", payload
                        )
                        print(
                            "[S2 Linked SP] Completed/returned "
                            f"procedure='{linked_proc_name}' result={s2_linked_result}"
                        )
                    except Exception as s2_proc_exc:
                        print(
                            "[S2 Linked SP] FAILED/conditions-not-met; "
                            "continuing to S3: "
                            f"procedure='{linked_proc_name}' error={s2_proc_exc}"
                        )
                else:
                    print(
                        "[S2 Linked SP] No configured S2 linked procedure; "
                        "continuing directly to mandatory S3 procedure."
                    )

                # Give SQL/API a short moment to commit any S2 procedure outputs
                # before the explicit S3 procedure reads the same row.
                print("[S2 Linked SP] Waiting 2 seconds before S3...")
                await asyncio.sleep(2)

                # Refresh the latest row values so S3 sees everything written
                # by the S2 linked procedure.
                refreshed_s2_context = await stage_service.get_stage_context(task_id, "S2")
                refreshed_s2_row = refreshed_s2_context.get("row") or {}

                s3_payload = dict(payload or {})
                s3_payload.setdefault("RowID", task_id)

                # SP_FMS_Pack2Dispatch_S3 commonly needs the Sales Order
                # DocEntry. Take it from the refreshed DB row when available.
                def _pick_s3_value(suffixes):
                    for source in (refreshed_s2_row,):
                        if isinstance(source, dict):
                            for key, value in source.items():
                                key_norm = "".join(ch for ch in str(key).lower() if ch.isalnum())
                                if any(key_norm.endswith("".join(ch for ch in str(s).lower() if ch.isalnum())) for s in suffixes):
                                    if value not in (None, ""):
                                        return value
                    return None

                i_doc_entry = _pick_s3_value(("idocentry", "docentry", "salesorderentry"))
                i_doc_num = _pick_s3_value(("idocnum", "docnum", "salesorderno"))

                if i_doc_entry is not None:
                    s3_payload.setdefault("I_DocEntry", i_doc_entry)
                    s3_payload.setdefault("DocEntry", i_doc_entry)
                    s3_payload.setdefault("DocEntrySO", i_doc_entry)
                if i_doc_num is not None:
                    s3_payload.setdefault("SalesOrderNo", i_doc_num)

                print(
                    "[S3 Linked SP] Running explicit downstream procedure "
                    "stage='S3' procedure='SP_FMS_Pack2Dispatch_S3'"
                )
                s3_result = await stage_service.run_named_stage_procedure(
                    task_id,
                    "S3",
                    "SP_FMS_Pack2Dispatch_S3",
                    {**s3_payload, "StageNo": "S3", "I_StageNo": 3},
                )
                print(
                    "[S3 Linked SP] Completed successfully "
                    f"procedure='SP_FMS_Pack2Dispatch_S3' result={s3_result}"
                )
                await stage_service.persist_s3_procedure_output(task_id, s3_result)

            else:
                # --------------------------------------------------------
                # EXISTING GENERIC FLOW FOR OTHER STAGES (INCLUDING S1)
                # --------------------------------------------------------
                current_stage_no = str(stage_no or "").strip()
                linked_stage_no = current_stage_no
                linked_stage = None

                current_order = None
                try:
                    current_order = int(str(current_stage_no).replace("S", "").replace("s", ""))
                except ValueError:
                    current_order = None

                for candidate in stage_configs:
                    candidate_stage_no = str((candidate or {}).get("STAGE_NO") or "").strip()
                    if not candidate_stage_no:
                        continue
                    if candidate_stage_no == current_stage_no:
                        continue
                    if current_order is not None:
                        try:
                            candidate_order = int(str(candidate_stage_no).replace("S", "").replace("s", ""))
                        except ValueError:
                            candidate_order = None
                        if candidate_order is not None and candidate_order > current_order:
                            linked_stage_no = candidate_stage_no
                            linked_stage = candidate
                            break
                    if linked_stage is None:
                        linked_stage_no = candidate_stage_no
                        linked_stage = candidate

                linked_proc_name = str(
                    (linked_stage or {}).get("Linked_Stored_Procedure")
                    or (linked_stage or {}).get("LINKED_STORED_PROCEDURE")
                    or (linked_stage or {}).get("linked_stored_procedure")
                    or (stage_context.get("stage") or {}).get("Linked_Stored_Procedure")
                    or (stage_context.get("stage") or {}).get("LINKED_STORED_PROCEDURE")
                    or (stage_context.get("stage") or {}).get("linked_stored_procedure")
                    or ""
                ).strip()
                print(
                    "[Linked SP] RESOLUTION: "
                    f"source_stage='{current_stage_no}' -> target_stage='{linked_stage_no}' "
                    f"procedure='{linked_proc_name or '<NONE>'}'"
                )

                linked_proc_succeeded = False
                linked_proc_result = None

                # For the custom Pack-to-Dispatch S1 form, NEVER allow the
                # generic linked-stage logic to execute a downstream procedure
                # before the mandatory S3 procedure. The S2 Linked_Stored_Procedure
                # is resolved and executed explicitly AFTER S3 below, using the
                # matching S2 FMS_Config CONDITIONS row.
                defer_custom_s1_s2 = (
                    current_stage_no == "S1"
                    and is_pick_pack_custom_s1
                )
                defer_custom_s3_s4 = (
                    current_stage_no == "S3"
                    and is_pick_pack_custom_s3
                )

                if defer_custom_s1_s2:
                    print(
                        "[Linked SP] DEFERRED: custom Pack-to-Dispatch S1 -> S2 "
                        "will be evaluated after mandatory S3 using I_SOShippingTypeU/FMS_Config."
                    )
                elif defer_custom_s3_s4:
                    print(
                        "[Linked SP] DEFERRED: custom Pack-to-Dispatch S3 downstream "
                        "procedure will run explicitly as SP_FMS_Pack2Dispatch_S4 "
                        "after SP_FMS_ALTimeStampStatus."
                    )
                elif linked_proc_name and linked_proc_name.lower() != "sp_fms_altimestampstatus":
                    print(
                        "[Linked SP] START: "
                        f"source_stage='{current_stage_no}' target_stage='{linked_stage_no}' "
                        f"procedure='{linked_proc_name}' RowID='{task_id}'"
                    )
                    try:
                        linked_proc_result = await stage_service.run_linked_stage_procedure(
                            task_id, linked_stage_no, payload
                        )
                        linked_proc_succeeded = True
                        print(
                            "[Linked SP] SUCCESS: "
                            f"procedure='{linked_proc_name}' result={linked_proc_result}"
                        )
                    except Exception as linked_proc_exc:
                        print(
                            "[Linked SP] FAILURE/CONDITION-NOT-MET: "
                            f"procedure='{linked_proc_name}' error={linked_proc_exc}"
                        )
                else:
                    print(
                        "[Linked SP] SKIPPED: no usable linked procedure configured "
                        f"for target_stage='{linked_stage_no}'"
                    )

                # --------------------------------------------------------
                # MANDATORY PICK/PACK S3 DOWNSTREAM ORDER
                # --------------------------------------------------------
                # Required order for this custom S3:
                #   1) SP_FMS_ALTimeStampStatus (already executed above)
                #   2) SP_FMS_Pack2Dispatch_S4
                if current_stage_no == "S3" and is_pick_pack_custom_s3 and not s3_s4_chain_executed:
                    print(
                        "[S3 DOWNSTREAM CHAIN] Required order: "
                        "SP_FMS_ALTimeStampStatus -> SP_FMS_Pack2Dispatch_S4"
                    )
                    await asyncio.sleep(2)

                    latest_s3_context = await stage_service.get_stage_context(task_id, "S3")
                    latest_s3_row = latest_s3_context.get("row") or {}

                    def _s3_pick_value(row, wanted):
                        if not isinstance(row, dict):
                            return None
                        wanted_norm = "".join(ch for ch in str(wanted).lower() if ch.isalnum())
                        for key, value in row.items():
                            key_norm = "".join(ch for ch in str(key).lower() if ch.isalnum())
                            if key_norm == wanted_norm and value not in (None, ""):
                                return value
                        return None

                    def _s3_pick_suffix(row, suffixes):
                        if not isinstance(row, dict):
                            return None
                        norm_suffixes = [
                            "".join(ch for ch in str(x).lower() if ch.isalnum())
                            for x in suffixes
                        ]
                        for key, value in row.items():
                            key_norm = "".join(ch for ch in str(key).lower() if ch.isalnum())
                            if any(key_norm.endswith(suf) for suf in norm_suffixes) and value not in (None, ""):
                                return value
                        return None

                    s4_payload = dict(payload or {})
                    s4_payload["RowID"] = task_id
                    s4_payload["StageNo"] = "S4"
                    s4_payload["I_StageNo"] = 4

                    i_doc_entry_s4 = _s3_pick_suffix(
                        latest_s3_row, ("idocentry", "docentry", "salesorderentry")
                    )
                    i_doc_num_s4 = _s3_pick_suffix(
                        latest_s3_row, ("idocnum", "docnum", "salesorderno")
                    )
                    if i_doc_entry_s4 is not None:
                        s4_payload.setdefault("I_DocEntry", i_doc_entry_s4)
                        s4_payload.setdefault("DocEntry", i_doc_entry_s4)
                        s4_payload.setdefault("DocEntrySO", i_doc_entry_s4)
                    if i_doc_num_s4 is not None:
                        s4_payload.setdefault("SalesOrderNo", i_doc_num_s4)

                    # Make S3 delivery inputs available to S4 under both the
                    # S3-prefixed column names and the conventional short names.
                    s3_del_doc = _s3_pick_value(latest_s3_row, "S3_DelDocNo") or s4_payload.get("S3_DelDocNo")
                    s3_del_line = _s3_pick_value(latest_s3_row, "S3_DelLineNo") or s4_payload.get("S3_DelLineNo")
                    if s3_del_doc is not None:
                        s4_payload.setdefault("S3_DelDocNo", s3_del_doc)
                        s4_payload.setdefault("DelDocNo", s3_del_doc)
                    if s3_del_line is not None:
                        s4_payload.setdefault("S3_DelLineNo", s3_del_line)
                        s4_payload.setdefault("DelLineNo", s3_del_line)

                    print(
                        "[S3 DOWNSTREAM CHAIN] Starting SP_FMS_Pack2Dispatch_S4: "
                        f"RowID='{task_id}', I_DocEntry={i_doc_entry_s4!r}, "
                        f"I_DocNum={i_doc_num_s4!r}, "
                        f"S3_DelDocNo={s3_del_doc!r}, S3_DelLineNo={s3_del_line!r}"
                    )

                    s4_result = await stage_service.run_named_stage_procedure(
                        task_id,
                        "S4",
                        "SP_FMS_Pack2Dispatch_S4",
                        s4_payload,
                    )
                    print(
                        "[S3 DOWNSTREAM CHAIN] SP_FMS_Pack2Dispatch_S4 completed: "
                        f"result={s4_result}"
                    )
                    if isinstance(s4_result, dict) and str(s4_result.get("status", "")).lower() == "error":
                        raise RuntimeError(
                            "SP_FMS_Pack2Dispatch_S4 failed: "
                            + str(s4_result.get("message") or s4_result)
                        )

                # --------------------------------------------------------
                # MANDATORY PICK/PACK S1 DOWNSTREAM ORDER
                # --------------------------------------------------------
                # Required order for this custom S1:
                #   1) SP_FMS_ALTimeStampStatus (already executed above)
                #   2) SP_FMS_Pack2Dispatch_S3 ALWAYS
                #   3) S2 Linked_Stored_Procedure ONLY when
                #      I_SOShippingTypeU == the S2 FMS_Config condition.
                #
                # IMPORTANT: SP_FMS_Pack2Dispatch_S4 MUST NOT run from S1.
                # S4 is triggered only when the user submits Stage S3.
                if (
                    current_stage_no == "S1"
                    and is_pick_pack_custom_s1
                ):
                    print(
                        "[S1 DOWNSTREAM CHAIN] Required order: "
                        "SP_FMS_Pack2Dispatch_S3 -> conditional S2 Linked_Stored_Procedure"
                    )

                    # ----------------------------------------------------
                    # STEP 2: ALWAYS RUN S3
                    # ----------------------------------------------------
                    print(
                        "[S1 DOWNSTREAM CHAIN] Starting mandatory "
                        "SP_FMS_Pack2Dispatch_S3"
                    )
                    await asyncio.sleep(2)

                    latest_s1_context = await stage_service.get_stage_context(task_id, "S1")
                    latest_s1_row = latest_s1_context.get("row") or {}

                    def _pick_value_case_insensitive(row, wanted):
                        if not isinstance(row, dict):
                            return None
                        wanted_norm = "".join(ch for ch in str(wanted).lower() if ch.isalnum())
                        for key, value in row.items():
                            key_norm = "".join(ch for ch in str(key).lower() if ch.isalnum())
                            if key_norm == wanted_norm and value not in (None, ""):
                                return value
                        return None

                    def _pick_suffix_case_insensitive(row, suffixes):
                        if not isinstance(row, dict):
                            return None
                        norm_suffixes = [
                            "".join(ch for ch in str(x).lower() if ch.isalnum())
                            for x in suffixes
                        ]
                        for key, value in row.items():
                            key_norm = "".join(ch for ch in str(key).lower() if ch.isalnum())
                            if any(key_norm.endswith(suf) for suf in norm_suffixes) and value not in (None, ""):
                                return value
                        return None

                    s3_payload = dict(payload or {})
                    s3_payload.setdefault("RowID", task_id)
                    s3_payload["StageNo"] = "S3"
                    s3_payload["I_StageNo"] = 3

                    i_doc_entry_s3 = _pick_suffix_case_insensitive(
                        latest_s1_row, ("idocentry", "docentry", "salesorderentry")
                    )
                    i_doc_num_s3 = _pick_suffix_case_insensitive(
                        latest_s1_row, ("idocnum", "docnum", "salesorderno")
                    )
                    if i_doc_entry_s3 is not None:
                        s3_payload.setdefault("I_DocEntry", i_doc_entry_s3)
                        s3_payload.setdefault("DocEntry", i_doc_entry_s3)
                        s3_payload.setdefault("DocEntrySO", i_doc_entry_s3)
                    if i_doc_num_s3 is not None:
                        s3_payload.setdefault("SalesOrderNo", i_doc_num_s3)

                    print(
                        "[S1 DOWNSTREAM CHAIN] S3 payload prepared: "
                        f"RowID='{task_id}', I_DocEntry={i_doc_entry_s3!r}, I_DocNum={i_doc_num_s3!r}"
                    )

                    s3_result = await stage_service.run_named_stage_procedure(
                        task_id,
                        "S3",
                        "SP_FMS_Pack2Dispatch_S3",
                        s3_payload,
                    )
                    print(
                        "[S1 DOWNSTREAM CHAIN] S3 completed: "
                        f"result={s3_result}"
                    )
                    await stage_service.persist_s3_procedure_output(task_id, s3_result)

                    # S4 is intentionally NOT executed from Stage S1.
                    # It runs only from the explicit Stage S3 submit flow below.
                    # Refresh the Stage-1 row after S3 so the condition is checked
                    # against the latest backend value.
                    refreshed_after_s3 = await stage_service.get_stage_context(task_id, "S1")
                    latest_s1_row = refreshed_after_s3.get("row") or latest_s1_row

                    # ----------------------------------------------------
                    # STEP 3: CONDITIONAL S2 LINKED PROCEDURE
                    # ----------------------------------------------------
                    # The physical row decides the shipping type. The
                    # matching FMS_Config S2 condition decides whether its
                    # Linked_Stored_Procedure should run.
                    shipping_type = _pick_value_case_insensitive(
                        latest_s1_row, "I_SOShippingTypeU"
                    )
                    shipping_norm = str(shipping_type or "").strip().lower()

                    print(
                        "[S1 DOWNSTREAM CHAIN] S2 condition check: "
                        f"I_SOShippingTypeU={shipping_type!r}"
                    )

                    matching_s2_configs = []
                    for cfg in stage_configs:
                        if not isinstance(cfg, dict):
                            continue
                        cfg_stage = str(
                            cfg.get("STAGE_NO")
                            or cfg.get("stage_no")
                            or ""
                        ).strip().upper()
                        cfg_condition = str(
                            cfg.get("conditions")
                            or cfg.get("CONDITIONS")
                            or ""
                        ).strip().lower()
                        cfg_proc = str(
                            cfg.get("Linked_Stored_Procedure")
                            or cfg.get("LINKED_STORED_PROCEDURE")
                            or cfg.get("linked_stored_procedure")
                            or ""
                        ).strip()
                        cfg_active = cfg.get("active") if "active" in cfg else cfg.get("ACTIVE")

                        if (
                            cfg_stage == "S2"
                            and cfg_condition
                            and cfg_condition == shipping_norm
                            and cfg_proc
                            and str(cfg_active).strip().lower() not in {"0", "false", "no", "n"}
                        ):
                            matching_s2_configs.append(cfg)

                    if shipping_norm and matching_s2_configs:
                        if len(matching_s2_configs) > 1:
                            print(
                                "[S1 DOWNSTREAM CHAIN] Multiple active S2 FMS_Config rows "
                                f"matched condition={shipping_type!r}; refusing to guess."
                            )
                        else:
                            s2_cfg = matching_s2_configs[0]
                            s2_proc_name = str(
                                s2_cfg.get("Linked_Stored_Procedure")
                                or s2_cfg.get("LINKED_STORED_PROCEDURE")
                                or s2_cfg.get("linked_stored_procedure")
                                or ""
                            ).strip()
                            print(
                                "[S1 DOWNSTREAM CHAIN] S2 condition MATCHED: "
                                f"I_SOShippingTypeU={shipping_type!r} -> "
                                f"procedure='{s2_proc_name}'"
                            )
                            s2_linked_result = await stage_service.run_linked_stage_procedure(
                                task_id, "S2", payload
                            )
                            print(
                                "[S1 DOWNSTREAM CHAIN] S2 Linked SP completed: "
                                f"procedure='{s2_proc_name}' result={s2_linked_result}"
                            )
                    else:
                        print(
                            "[S1 DOWNSTREAM CHAIN] S2 Linked SP SKIPPED: "
                            f"I_SOShippingTypeU={shipping_type!r}; "
                            "no active S2 FMS_Config condition matched."
                        )

            # ====================================================================
            # FINAL ROW-CYCLE STATUS UPDATE
            #
            # Whenever SP_FMS_ALTimeStampStatus ran, this procedure MUST be the
            # final stored procedure for the submission. It is intentionally
            # placed after every S1/S2/S3/S4/linked-SP branch above.
            # ====================================================================
            if altimestamp_sp_ran:
                print(
                    "[FINAL ROW CYCLE] Starting SP_FMS_Pack2Dispatch_RowCycleStatus "
                    "after all stage stored procedures have completed."
                )

                row_cycle_errors: list[str] = []
                row_cycle_row_ids: list[str] = []
                for candidate in [str(task_id), *[str(r) for r in related_row_ids]]:
                    candidate = str(candidate or "").strip()
                    if candidate and candidate not in row_cycle_row_ids:
                        row_cycle_row_ids.append(candidate)

                for row_id_for_cycle in row_cycle_row_ids:
                    cycle_payload = {
                        "RowID": row_id_for_cycle,
                        "AppID": app_id_for_sp,
                        "StageNo": stage_no_for_sp,
                    }
                    print(
                        "[FINAL ROW CYCLE] Running "
                        f"RowID='{row_id_for_cycle}', AppID='{app_id_for_sp}', "
                        f"StageNo='{stage_no_for_sp}'"
                    )
                    try:
                        cycle_result = await stage_service.run_named_stage_procedure(
                            row_id_for_cycle,
                            stage_no_for_sp,
                            "SP_FMS_Pack2Dispatch_RowCycleStatus",
                            cycle_payload,
                        )
                        print(
                            "[FINAL ROW CYCLE] Completed: "
                            f"RowID='{row_id_for_cycle}', Result={cycle_result!r}"
                        )
                        if isinstance(cycle_result, dict):
                            status_text = str(
                                cycle_result.get("status") or ""
                            ).strip().casefold()
                            if (
                                status_text in {"error", "failed", "failure"}
                                or cycle_result.get("success") is False
                            ):
                                row_cycle_errors.append(
                                    f"RowID {row_id_for_cycle}: "
                                    f"{cycle_result.get('message') or cycle_result}"
                                )
                    except Exception as row_cycle_exc:
                        row_cycle_errors.append(
                            f"RowID {row_id_for_cycle}: {row_cycle_exc}"
                        )
                        print(
                            "[FINAL ROW CYCLE] FAILED: "
                            f"RowID='{row_id_for_cycle}', Error={row_cycle_exc!r}"
                        )

                if row_cycle_errors:
                    raise RuntimeError(
                        "SP_FMS_Pack2Dispatch_RowCycleStatus failed: "
                        + " | ".join(row_cycle_errors)
                    )

                print(
                    "[FINAL ROW CYCLE] COMPLETE: "
                    "RowCycleStatus was the final SP in the submission flow."
                )

        except Exception as proc_exc:
            print(f"Linked stored procedure run failed for {task_id}/{stage_no}: {proc_exc}")
            raise
    
    # Stage 1 completion changes picker availability immediately because the
    # picker becomes free once S1 Actual Timestamp is written.
    if str(stage_no or "").strip().upper() in {"1", "S1"} or str(stage_no or "").strip().upper().startswith("S1"):
        stage1_custom_service.invalidate_picker_assignment_cache()

    if action in {"submit", "append"}:
        return RedirectResponse(
            route_builder.stage_form_route(task_id, stage_no, app_id=str(fms_info.get("app_id") or requested_app_id or ""))
            + "&saved=1&appended=1",
            status_code=303,
        )

    return RedirectResponse(
        route_builder.stage_form_route(task_id, stage_no, app_id=str(fms_info.get("app_id") or requested_app_id or ""))
        + "&saved=1",
        status_code=303,
    )


@router.post("/forms/stage/{task_id}/{stage_no}/clear-responses", response_class=HTMLResponse)
async def stage_form_clear_responses(request: Request, task_id: str, stage_no: str, app_id: str = ""):
    # Clear & Reset is intentionally available to Guest as well as logged-in roles.
    # The action only clears this stage's editable/runtime data; planned fields remain intact.

    stage_context = await stage_service.get_stage_context(task_id, stage_no, app_id=(str(app_id).strip() or None))
    clear_payload = _build_stage_clear_payload(stage_context, stage_no)
    employee_code = _get_authenticated_employee_code(request)
    _inject_update_audit_fields(clear_payload, stage_context.get("row") or {}, employee_code)

    try:
        clear_target_row_ids = {str(task_id)}
        related_row_ids = await _resolve_related_salesorder_row_ids(stage_context, task_id)
        clear_target_row_ids.update(str(row_id) for row_id in related_row_ids if str(row_id).strip())

        for clear_row_id in sorted(clear_target_row_ids, key=lambda x: str(x)):
            try:
                await table_api.update_row(stage_context["fms"]["table_name"], clear_row_id, clear_payload)
            except Exception as row_exc:
                print(f"Failed to clear related form row {clear_row_id}: {row_exc}")

        # Child PickNPack batch rows belong exclusively to Stage 1.
        # Never touch them when Clear & Reset is invoked for S2-S6.
        if _normalize_name(stage_no) == "s1":
            row = stage_context.get("row") or {}
            doc_num = _to_text(_row_value_by_suffix(row, ("idocnum", "docnum", "salesorderno")))
            create_ts = _to_text(_row_value_by_suffix(row, ("icreatets", "createts", "createtimestamp", "createdts")))

            custom_stage1 = await stage1_custom_service.build_stage1_custom_context(stage_context)
            clear_batch_row_ids: set[str] = set()
            for batch in custom_stage1.get("batches", []):
                row_id = str(batch.get("row_id") or "").strip()
                if row_id:
                    clear_batch_row_ids.add(row_id)

            if doc_num:
                for filters in (
                    {"I_DocNum": doc_num, "I_CreateTS": create_ts} if create_ts else {"I_DocNum": doc_num},
                    {"DocNum": doc_num, "CreateTS": create_ts} if create_ts else {"DocNum": doc_num},
                    {"I_DocNum": doc_num},
                    {"DocNum": doc_num},
                ):
                    try:
                        batch_rows = await table_api.get_table("Z_SOPickNpackBatchDetails", filters=filters)
                        for batch_row in batch_rows or []:
                            batch_row_id = str(_row_value_by_suffix(batch_row, ("rowid", "id", "pk")) or "").strip()
                            if batch_row_id:
                                clear_batch_row_ids.add(batch_row_id)
                    except Exception as fetch_exc:
                        print(f"Failed to fetch batch rows for clear {filters}: {fetch_exc}")

            for row_id in sorted(clear_batch_row_ids, key=lambda x: str(x)):
                child_payload = {
                    "action": "update",
                    "table": "Z_SOPickNpackBatchDetails",
                    "data": {
                        "S1_Submission": None,
                        "S1_Remarks": None,
                    },
                    "where": {
                        "RowID": row_id,
                    },
                }
                try:
                    await api_client.post("update_row", json_body=child_payload)
                except Exception as child_exc:
                    print(f"Failed to clear batch row {row_id}: {child_exc}")
    except Exception as exc:
        err_msg = quote_plus(str(exc).strip() or "Unable to clear stage responses")
        return RedirectResponse(
            f"{route_builder.stage_form_route(task_id, stage_no)}?clear_error={err_msg}",
            status_code=303,
        )

    # Clear & Reset can reopen Stage 1, so invalidate the picker availability
    # cache immediately instead of waiting for the short TTL to expire.
    if str(stage_no or "").strip().upper() in {"1", "S1"} or str(stage_no or "").strip().upper().startswith("S1"):
        stage1_custom_service.invalidate_picker_assignment_cache()

    return RedirectResponse(
        route_builder.stage_form_route(task_id, stage_no, app_id=str((stage_context.get("fms") or {}).get("app_id") or app_id or ""))
        + "&cleared=1",
        status_code=303,
    )


@router.post("/forms/stage/{task_id}/{stage_no}/lead-time-grant")
async def stage_lead_time_grant_submit(request: Request, task_id: str, stage_no: str, app_id: str = ""):
    stage_context = await stage_service.get_stage_context(task_id, stage_no, app_id=(str(app_id).strip() or None))
    if not stage_context.get("editable"):
        return RedirectResponse(
            f"{route_builder.stage_form_route(task_id, stage_no)}?ltg_error=Stage+is+not+editable",
            status_code=303,
        )

    custom_stage1 = await stage1_custom_service.build_stage1_custom_context(stage_context)
    stage_context["custom_stage1"] = custom_stage1

    form_data = await request.form()
    remarks = str(form_data.get("lead_time_grant_remarks") or "").strip()
    if not remarks:
        return RedirectResponse(
            f"{route_builder.stage_form_route(task_id, stage_no)}?ltg_error=Lead+Time+Grant+Remarks+is+required",
            status_code=303,
        )
    app_id_raw = str(form_data.get("app_id") or "").strip()
    doer_name_raw = str(form_data.get("doer_name") or "").strip()
    doer_name = doer_name_raw or _pick_stage_doer_value(stage_context) or "-"
    fms_row_id_raw = str(form_data.get("fms_row_id") or "").strip()
    requested_minutes_raw = str(form_data.get("requested_minutes") or "").strip()
    if not requested_minutes_raw.isdigit():
        return RedirectResponse(
            f"{route_builder.stage_form_route(task_id, stage_no)}?ltg_error=Requested+Minutes+must+be+a+whole+number",
            status_code=303,
        )
    requested_minutes = int(requested_minutes_raw)
    if requested_minutes <= 0:
        return RedirectResponse(
            f"{route_builder.stage_form_route(task_id, stage_no)}?ltg_error=Requested+Minutes+must+be+greater+than+0",
            status_code=303,
        )

    fms_info = stage_context.get("fms", {})
    app_id = app_id_raw or str(fms_info.get("app_id", "GENERAL") or "GENERAL")
    current_year = datetime.now().strftime("%Y")
    attachment_folder_name = fms_info.get("attachment_folder") or f"{current_year}_{app_id}_ATTACHMENTS"
    upload_base_dir = Path("app/static/uploads") / attachment_folder_name / "lead_time_grant"
    upload_base_dir.mkdir(parents=True, exist_ok=True)

    attachment_urls: list[str] = []
    files = form_data.getlist("attachments")
    for item in files:
        if item is None or not hasattr(item, "filename") or not hasattr(item, "read"):
            continue
        file_name = str(getattr(item, "filename", "") or "").strip()
        if not file_name:
            continue
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        original_name = Path(file_name).name
        normalized_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)
        safe_filename = f"{timestamp}_{normalized_name}"
        file_path = upload_base_dir / safe_filename
        contents = await item.read()
        with open(file_path, "wb") as f:
            f.write(contents)
        attachment_urls.append(f"/static/uploads/{attachment_folder_name}/lead_time_grant/{safe_filename}")

    stage_name = str((stage_context.get("stage") or {}).get("STAGE_NAME") or "")
    fms_name = str((stage_context.get("fms") or {}).get("fms_name") or "")
    stage_no_int = _extract_stage_no_int(stage_no)
    primary_key = str(stage_context.get("primary_key") or "").strip()
    row_data = stage_context.get("row") or {}
    fms_row_id = fms_row_id_raw or str(row_data.get(primary_key) or task_id or "").strip()
    insert_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        lead_time_table = await _resolve_lead_time_grant_table()
        existing_requests = await table_api.get_table(
            lead_time_table,
            filters={
                "I_FMS_Name": fms_name,
                "I_Stage_No": stage_no_int,
                "I_FMS_Row_ID": fms_row_id,
                "I_Doer_Name": doer_name,
                "I_Status": "Pending"
            }
        )
        
        if existing_requests and len(existing_requests) > 0:
            return RedirectResponse(
                f"{route_builder.stage_form_route(task_id, stage_no)}?ltg_error=Pehle+wali+request+abhi+pending+hai.+Naya+form+fill+nahi+kar+sakte.",
                status_code=303,
            )
    except Exception as check_exc:
        print(f"Error checking existing requests: {check_exc}")

    insert_payload = {
        "I_APPID": app_id,
        "I_FMS_Name": fms_name,
        "I_Stage_No": stage_no_int,
        "I_Stage_Name": stage_name,
        "I_Doer_Name": doer_name,
        "I_Requested_Min": requested_minutes,
        "I_Attachments": ",".join(attachment_urls) if attachment_urls else None,
        "I_Lead_Time_Grant_Remarks": remarks or None,
        "I_InsertTS": insert_ts,
        "I_FMS_Row_ID": fms_row_id or None,
        "I_Created_By_EmpID": _get_authenticated_employee_code(request),
        "I_Status": "Pending" 
    }

    try:
        lead_time_table = await _resolve_lead_time_grant_table()
        await table_api.insert_row(lead_time_table, insert_payload)
    except Exception as exc:
        err_msg = quote_plus(str(exc).strip() or "Unable to save Lead Time Extention")
        return RedirectResponse(
            f"{route_builder.stage_form_route(task_id, stage_no)}?ltg_error={err_msg}",
            status_code=303,
        )

    return RedirectResponse(
        f"{route_builder.stage_form_route(task_id, stage_no)}?ltg_saved=1",
        status_code=303,
    )


@router.get("/forms/dropdown-options")
async def dropdown_options(table: str, select_col: str, filter_col: str = "", filter_val: str = ""):
    """Internal route called by JS to fetch dropdown values dynamically."""
    from app.api.client import api_client
    params = {
        "action": "get_dependent_list",
        "table": table,
        "select_column": select_col,
        "filter_column": filter_col,
        "filter_value": filter_val
    }
    data = await api_client.get("get_dependent_list", query_params=params)
    return data.get("data", []) if isinstance(data, dict) else []
