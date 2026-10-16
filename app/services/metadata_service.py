
"""
Configuration-table access: FMS_Config + TM_Application_Master, plus
table metadata helpers used by the dynamic form renderer.

Every application table name comes from FMS_Config.TABLE_NAME - nothing
here (or anywhere else) hardcodes a table.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from app.api import metadata_api, table_api

FMS_CONFIG_TABLE = "FMS_Config"
APPLICATION_MASTER_TABLE = "TM_Application_Master"

logger = logging.getLogger("fms_portal.metadata")

# Configuration/metadata changes are infrequent compared with form reads.
# Keep a short in-process cache so every form does not repeatedly read the
# same FMS_Config / TM_Form_Settings / INFORMATION_SCHEMA data.
_CACHE_TTL = 15.0
_METADATA_TTL = 60.0
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_locks: dict[tuple, asyncio.Lock] = {}

def _cached(key: tuple):
    item = _cache.get(key)
    if item and (time.monotonic() - item[0]) < (_METADATA_TTL if key[0] == "metadata" else _CACHE_TTL):
        return item[1]
    return None

def _lock_for(key: tuple) -> asyncio.Lock:
    lock = _cache_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _cache_locks[key] = lock
    return lock


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _pick(row: dict[str, Any], *aliases: str) -> Any:
    normalized = {_normalize_key(k): v for k, v in row.items()}
    for alias in aliases:
        key = _normalize_key(alias)
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    return None


def _pick_str(row: dict[str, Any], *aliases: str) -> str:
    value = _pick(row, *aliases)
    if value is None:
        return ""
    return str(value).strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _is_truthy(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _normalize_fms_row(row: dict[str, Any]) -> dict[str, Any]:
    """Augment a raw FMS_Config row with canonical keys used by services/UI."""
    normalized = dict(row)
    normalized["APP_ID"] = _pick_str(row, "APP_ID", "app_id")
    normalized["FMS_NAME"] = _pick_str(row, "FMS_NAME", "FMS Name", "fms_name")
    normalized["FMS_TYPE"] = _pick_str(row, "FMS_TYPE", "FMS Type", "fms_type")
    normalized["TABLE_NAME"] = _pick_str(row, "TABLE_NAME", "FMS_Table_Name", "fms_table_name")
    normalized["STAGE_NO"] = _pick_str(row, "STAGE_NO", "stage_no", "stage no")
    normalized["STAGE_NAME"] = _pick_str(row, "STAGE_NAME", "stage_name", "stage name")
    normalized["DOER"] = _pick_str(row, "DOER", "doer_name", "doer")
    normalized["DOER_EMP_CODE"] = _pick_str(row, "DOER_EMP_CODE", "AllocatedDoer_EC", "doer_ec")
    normalized["BACKUP_DOER"] = _pick_str(row, "BACKUP_DOER", "backup_doer")
    normalized["BACKUP_EMP_CODE"] = _pick_str(row, "BACKUP_EMP_CODE", "BackupDoer_EC", "backup_ec")
    normalized["PLANNED_COLUMN"] = _pick_str(row, "PLANNED_COLUMN", "planned_column")
    normalized["ACTUAL_COLUMN"] = _pick_str(row, "ACTUAL_COLUMN", "actual_column")
    normalized["PROCESS_COORDINATOR"] = _pick_str(
        row, "Process_Corrdinator", "PROCESS_COORDINATOR", "Process Coordinator", "process_coordinator"
    )
    normalized["STAGE_FORM_LINK"] = _pick_str(
        row, "Stage_Form_Link", "STAGE_FORM_LINK", "Stage Form Link", "stage_form_link"
    )
    normalized["INPUT_GUIDE"] = _pick_str(row, "InputGuide", "Input Guide", "INPUT_GUIDE")
    normalized["DISPLAY_ORDER"] = _to_int(_pick(row, "DISPLAY_ORDER", "display_order"), default=0)
    return normalized


def invalidate_fms_config_cache(app_id: str | None = None) -> None:
    """Invalidate cached FMS_Config rows after a developer configuration save."""
    target = str(app_id or "")
    keys = [key for key in _cache if key and key[0] == "fms_config" and (not target or key[1] == target)]
    for key in keys:
        _cache.pop(key, None)


async def get_fms_config(app_id: str | None = None, fms_type: str | None = None) -> list[dict[str, Any]]:
    """Return cached, normalized FMS configuration with a short TTL."""
    normalized_type = str(fms_type or "").strip().upper()
    key = ("fms_config", str(app_id or ""), normalized_type)
    cached = _cached(key)
    if cached is not None:
        return cached

    async with _lock_for(key):
        cached = _cached(key)
        if cached is not None:
            return cached

        raw_rows: list[dict[str, Any]] = []
        if normalized_type:
            for type_column in ("FMS_TYPE", "FMS Type", "fms_type"):
                try:
                    candidate_rows = await table_api.get_table(FMS_CONFIG_TABLE, filters={type_column: normalized_type})
                    candidate_normalized = [_normalize_fms_row(r) for r in candidate_rows]
                    if any(str(r.get("FMS_TYPE") or "").strip().upper() == normalized_type for r in candidate_normalized):
                        raw_rows = candidate_rows
                        break
                except Exception as exc:
                    logger.debug("FMS_Config filter '%s' unavailable: %s", type_column, exc)

        if not raw_rows:
            raw_rows = await table_api.get_table(FMS_CONFIG_TABLE)

        rows = [_normalize_fms_row(r) for r in raw_rows]
        if app_id is not None:
            target_app_id = str(app_id).strip()
            rows = [r for r in rows if str(r.get("APP_ID") or "").strip() == target_app_id]
        if normalized_type:
            rows = [r for r in rows if str(r.get("FMS_TYPE") or "").strip().upper() == normalized_type]

        _cache[key] = (time.monotonic(), rows)
        return rows


async def get_application_master(app_id: str | None = None) -> list[dict[str, Any]]:
    key = ("app_master", str(app_id or ""))
    cached = _cached(key)
    if cached is not None:
        return cached
    async with _lock_for(key):
        cached = _cached(key)
        if cached is not None:
            return cached
        rows = await table_api.get_table(APPLICATION_MASTER_TABLE)
        if app_id is not None:
            rows = [r for r in rows if str(r.get("APP_ID")) == str(app_id)]
        _cache[key] = (time.monotonic(), rows)
        return rows


def _group_by_app(config_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in config_rows:
        fms_name = str(row.get("FMS_NAME") or "").strip()
        if not fms_name:
            continue
        key = fms_name.lower()
        display_order = _to_int(row.get("DISPLAY_ORDER"), default=0)
        if key not in grouped:
            grouped[key] = {
                "app_id": row.get("APP_ID"),
                "fms_name": fms_name,
                "fms_type": row.get("FMS_TYPE"),
                "table_name": row.get("TABLE_NAME"),
                "display_order": display_order,
            }
            continue

        # Keep a stable app/table and the lowest display order for sorting.
        if not grouped[key].get("app_id") and row.get("APP_ID"):
            grouped[key]["app_id"] = row.get("APP_ID")
        if not grouped[key].get("table_name") and row.get("TABLE_NAME"):
            grouped[key]["table_name"] = row.get("TABLE_NAME")
        grouped[key]["display_order"] = min(_to_int(grouped[key].get("display_order"), 0), display_order)

    return sorted(
        grouped.values(),
        key=lambda x: (_to_int(x.get("display_order"), 0), str(x.get("fms_name") or "").lower()),
    )


async def get_dashboard_fms_options() -> list[dict[str, Any]]:
    """Distinct applications where FMS_TYPE='F' - the dashboard's FMS dropdown."""
    # Fetch only F-type configuration rows. This is the critical dashboard
    # source now that the dashboard FMS type has changed from D to F.
    dashboard_rows = await get_fms_config(fms_type="F")
    all_rows = dashboard_rows
    options = _group_by_app(dashboard_rows)
    options = sorted(options, key=lambda x: str(x.get("fms_name") or "").lower())

    distinct_names = ", ".join([str(opt.get("fms_name") or "") for opt in options])
    logger.info("Loaded %d rows from FMS_Config", len(all_rows))
    logger.info("After FMS_TYPE='F': %d rows", len(dashboard_rows))
    logger.info("Distinct FMS: %s", distinct_names or "(none)")
    logger.info("Dropdown Count: %d", len(options))
    logger.info("Final dropdown list: %s", [str(opt.get("fms_name") or "") for opt in options])

    return options


async def get_all_fms_options() -> list[dict[str, Any]]:
    """Every distinct application registered in FMS_Config, any FMS_TYPE."""
    config_rows = await get_fms_config()
    return _group_by_app(config_rows)


async def get_table_metadata(table_name: str) -> list[dict[str, Any]]:
    return await metadata_api.get_metadata(table_name)


async def get_dashboard_filter_columns(table_name: str) -> list[dict[str, str]]:
    key = ("filter_columns", table_name.strip().lower())
    cached = _cached(key)
    if cached is not None:
        return cached
    async with _lock_for(key):
        cached = _cached(key)
        if cached is not None:
            return cached
        rows = await table_api.get_table("TM_Form_Settings", filters={"TABLE_NAME": table_name})
        columns: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            column_name = _pick_str(row, "COLUMN_NAME", "column_name")
            if not column_name or column_name.lower() in seen:
                continue
            if not _is_truthy(_pick(row, "APPLY_FILTER", "apply_filter")):
                continue
            publish_name = _pick_str(row, "PUBLISH_NAME", "publish_name")
            columns.append({"column_name": column_name, "label": publish_name or column_name})
            seen.add(column_name.lower())
        columns.sort(key=lambda item: item["label"].lower())
        _cache[key] = (time.monotonic(), columns)
        return columns


async def resolve_primary_key(table_name: str) -> str:
    key = ("primary_key", table_name.strip().lower())
    cached = _cached(key)
    if cached is not None:
        return cached
    async with _lock_for(key):
        cached = _cached(key)
        if cached is not None:
            return cached
        columns = await get_table_metadata(table_name)
        primary = next((c["name"] for c in columns if c.get("is_primary_key")), None)
        if not primary:
            primary = next((c["name"] for c in columns if (c.get("name") or "").lower() in ("taskid", "task_id", "id")), None)
        if not primary:
            primary = next((c["name"] for c in columns if (c.get("name") or "").lower().endswith("id")), None)
        primary = primary or (columns[0]["name"] if columns else "TaskID")
        _cache[key] = (time.monotonic(), primary)
        return primary
