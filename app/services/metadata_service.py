
"""
Configuration-table access: FMS_Config + TM_Application_Master, plus
table metadata helpers used by the dynamic form renderer.

Every application table name comes from FMS_Config.TABLE_NAME - nothing
here (or anywhere else) hardcodes a table.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import re
import time
from typing import Any

from app.api import metadata_api, table_api
from app.api.client import get_active_api_base, get_effective_database_target
from app.auth.request_context import reset_current_database_target, set_current_database_target

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


def _cache_scope() -> str:
    """Scope in-process metadata caches to the active API target (API_1 vs API_2)."""
    return get_active_api_base()


def is_app_live_value(value: Any) -> bool:
    """Return True iff TM_Application_Master.Live represents Live = 'Y'."""
    return str(value or "").strip().upper() == "Y"


def is_app_non_live_value(value: Any) -> bool:
    """Return True iff TM_Application_Master.Live is 'N', NULL, or blank (not 'Y')."""
    return not is_app_live_value(value)


def extract_app_live(row: dict[str, Any]) -> str:
    """Extract normalized TM_Application_Master.Live value ('Y', 'N', or '')."""
    raw = _pick_str(row, "Live", "LIVE", "live", "Live_Input", "LiveInput")
    cleaned = raw.strip().upper()
    if cleaned in {"NULL", "NONE", "-"}:
        return ""
    return cleaned

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

    # Doer resolution per SDC_FMSSetup_AllocatedDoerRetirement (09 Nov 2026):
    # 1. Temp doer (if configured, both ends of cover window filled, present moment falls inside it)
    # 2. Otherwise permanent doer from Doer_EC / Doer
    # 3. Otherwise EA Operations & Development ('0195')
    perm_doer = _pick_str(row, "DOER", "doer_name", "doer")
    perm_doer_ec = _pick_str(row, "DOER_EMP_CODE", "Doer_EC", "doer_ec")
    temp_doer = _pick_str(row, "TEMP_DOER", "Temp_Doer", "temp_doer", "TempDoer")
    temp_doer_ec = _pick_str(row, "TEMP_DOER_EMP_CODE", "TempDoer_EC", "temp_doer_ec", "tempdoerec")
    from_dt_raw = _pick_str(row, "I_FromDateTime", "from_date_time", "fromdatetime")
    to_dt_raw = _pick_str(row, "I_ToDateTime", "to_date_time", "todatetime")

    resolved_doer = perm_doer
    resolved_doer_ec = perm_doer_ec

    if temp_doer_ec and from_dt_raw and to_dt_raw:
        try:
            now = datetime.now()
            from_dt = None
            to_dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                if not from_dt:
                    try:
                        from_dt = datetime.strptime(from_dt_raw, fmt)
                    except ValueError:
                        pass
                if not to_dt:
                    try:
                        to_dt = datetime.strptime(to_dt_raw, fmt)
                    except ValueError:
                        pass
            if from_dt and to_dt and from_dt <= now <= to_dt:
                resolved_doer = temp_doer or temp_doer_ec
                resolved_doer_ec = temp_doer_ec
        except Exception:
            pass

    if not resolved_doer_ec and not resolved_doer:
        resolved_doer_ec = "0195"
        resolved_doer = "EA Operations & Development"

    normalized["DOER"] = resolved_doer
    normalized["DOER_EMP_CODE"] = resolved_doer_ec
    normalized["PERMANENT_DOER"] = perm_doer
    normalized["PERMANENT_DOER_EMP_CODE"] = perm_doer_ec
    normalized["TEMP_DOER"] = temp_doer
    normalized["TEMP_DOER_EMP_CODE"] = temp_doer_ec
    normalized["I_FROM_DATE_TIME"] = from_dt_raw
    normalized["I_TO_DATE_TIME"] = to_dt_raw
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
    keys = [
        key for key in _cache
        if key and key[0] == "fms_config" and (not target or (len(key) >= 3 and key[2] == target))
    ]
    for key in keys:
        _cache.pop(key, None)


def invalidate_application_master_cache() -> None:
    """Invalidate cached TM_Application_Master rows across all API scopes."""
    keys = [key for key in _cache if key and key[0] == "app_master"]
    for key in keys:
        _cache.pop(key, None)


async def get_fms_config(app_id: str | None = None, fms_type: str | None = None) -> list[dict[str, Any]]:
    """Return cached, normalized FMS configuration with a short TTL."""
    normalized_type = str(fms_type or "").strip().upper()
    key = ("fms_config", _cache_scope(), str(app_id or ""), normalized_type)
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
    key = ("app_master", _cache_scope(), str(app_id or ""))
    cached = _cached(key)
    if cached is not None:
        return cached
    async with _lock_for(key):
        cached = _cached(key)
        if cached is not None:
            return cached
        rows = await table_api.get_table(APPLICATION_MASTER_TABLE)
        if app_id is not None:
            target_app_id = str(app_id).strip().upper()
            rows = [
                r for r in rows
                if _pick_str(r, "APP_ID", "app_id", "AppID", "App_ID").upper() == target_app_id
            ]
        _cache[key] = (time.monotonic(), rows)
        return rows


async def get_application_live_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Build case-normalized Live lookups from TM_Application_Master for the active DB."""
    rows = await get_application_master()
    live_by_app_id: dict[str, str] = {}
    live_by_app_name: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        live_val = extract_app_live(row)
        row_app_id = _pick_str(row, "APP_ID", "app_id", "AppID", "App_ID")
        row_app_name = _pick_str(row, "APP_NAME", "app_name", "AppName", "App_Name")
        if row_app_id:
            live_by_app_id[row_app_id.upper()] = live_val
        if row_app_name:
            live_by_app_name[row_app_name.lower()] = live_val
    return live_by_app_id, live_by_app_name


def _resolve_live_from_maps(
    app_id: Any,
    fms_name: Any,
    live_by_app_id: dict[str, str],
    live_by_app_name: dict[str, str],
) -> str:
    app_id_key = str(app_id or "").strip().upper()
    if app_id_key and app_id_key in live_by_app_id:
        return live_by_app_id[app_id_key]
    fms_name_key = str(fms_name or "").strip().lower()
    if fms_name_key and fms_name_key in live_by_app_name:
        return live_by_app_name[fms_name_key]
    return ""


async def lookup_app_live_status(
    app_id: str | None = None,
    fms_name: str | None = None,
    table_name: str | None = None,
) -> str:
    """Resolve an FMS application's Live flag ('Y', 'N', or '') from TM_Application_Master."""
    app_id_clean = str(app_id or "").strip()
    fms_name_clean = str(fms_name or "").strip()
    table_name_clean = str(table_name or "").strip()

    if table_name_clean and (not app_id_clean or not fms_name_clean):
        try:
            config_rows = await get_fms_config()
            target_table = table_name_clean.replace("[", "").replace("]", "").rsplit(".", 1)[-1].lower()
            for row in config_rows:
                row_table = str(row.get("TABLE_NAME") or "").strip().replace("[", "").replace("]", "").rsplit(".", 1)[-1].lower()
                if row_table and row_table == target_table:
                    if not app_id_clean and row.get("APP_ID"):
                        app_id_clean = str(row.get("APP_ID")).strip()
                    if not fms_name_clean and row.get("FMS_NAME"):
                        fms_name_clean = str(row.get("FMS_NAME")).strip()
                    break
        except Exception as exc:
            logger.debug("FMS_Config table_name lookup failed for '%s': %s", table_name_clean, exc)

    live_by_app_id, live_by_app_name = await get_application_live_maps()
    return _resolve_live_from_maps(app_id_clean, fms_name_clean, live_by_app_id, live_by_app_name)


async def filter_fms_options_for_active_db(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter FMS options according to the selected One Portal DB and TM_Application_Master.Live.

    - OperationsDB (Production): show ONLY FMS with Live = 'Y'.
    - Test_OperationsDB (Test): show ALL FMS (Live = 'Y', 'N', NULL, or blank).
      Per-FMS Pull/Append routing still directs Live = 'Y' to apiv3.php and
      Live = 'N'/NULL/blank to apiv2.php in both environments.
    """
    active_db = get_effective_database_target()
    live_by_app_id, live_by_app_name = await get_application_live_maps()
    filtered: list[dict[str, Any]] = []
    for option in options:
        live_val = _resolve_live_from_maps(
            option.get("app_id"),
            option.get("fms_name"),
            live_by_app_id,
            live_by_app_name,
        )
        is_live = is_app_live_value(live_val)
        include = is_live if active_db == "OperationsDB" else True
        if include:
            filtered.append({
                **option,
                "live": live_val,
                "target_db": "OperationsDB" if is_live else "Test_OperationsDB",
            })

    logger.info(
        "Filtered FMS options for %s (%s): %d -> %d (%s)",
        active_db,
        get_active_api_base(),
        len(options),
        len(filtered),
        [str(opt.get("fms_name") or "") for opt in filtered],
    )
    return filtered


async def resolve_database_target_for_app(
    app_id: str | None = None,
    fms_name: str | None = None,
    table_name: str | None = None,
    live_value: Any = None,
) -> str:
    """Resolve the Pull/Append database target for an FMS app based on TM_Application_Master.Live.

    Applies in BOTH OperationsDB and Test_OperationsDB:
    - Live = 'Y' -> OperationsDB (API_2_URL / apiv3.php)
    - Live = 'N', NULL, or blank -> Test_OperationsDB (API_1_URL / apiv2.php)
    """
    effective_db = get_effective_database_target()
    if live_value is not None:
        return "OperationsDB" if is_app_live_value(live_value) else "Test_OperationsDB"

    if not any(str(v or "").strip() for v in (app_id, fms_name, table_name)):
        return effective_db

    try:
        live_val = await lookup_app_live_status(
            app_id=app_id,
            fms_name=fms_name,
            table_name=table_name,
        )
        resolved_db = "OperationsDB" if is_app_live_value(live_val) else "Test_OperationsDB"
        if resolved_db != effective_db:
            logger.info(
                "App (app_id=%r, fms_name=%r, table_name=%r) has Live=%r in %s; "
                "routing Pull/Append to %s (%s)",
                app_id,
                fms_name,
                table_name,
                live_val or None,
                effective_db,
                resolved_db,
                "API_2 / apiv3.php" if resolved_db == "OperationsDB" else "API_1 / apiv2.php",
            )
        return resolved_db
    except Exception as exc:
        logger.warning(
            "Unable to verify TM_Application_Master.Live for app_id=%r fms_name=%r table_name=%r: %s",
            app_id,
            fms_name,
            table_name,
            exc,
        )
        return effective_db


@asynccontextmanager
async def app_live_database_context(
    app_id: str | None = None,
    fms_name: str | None = None,
    table_name: str | None = None,
    live_value: Any = None,
):
    """Route Pull/Append calls to apiv3.php (Live='Y') or apiv2.php (Live='N'/NULL/blank) in both DB modes."""
    effective_db = get_effective_database_target()
    if live_value is None and not any(str(v or "").strip() for v in (app_id, fms_name, table_name)):
        yield effective_db
        return

    resolved_db = await resolve_database_target_for_app(
        app_id=app_id,
        fms_name=fms_name,
        table_name=table_name,
        live_value=live_value,
    )
    if resolved_db == effective_db:
        yield resolved_db
        return

    token = set_current_database_target(resolved_db)
    try:
        yield resolved_db
    finally:
        reset_current_database_target(token)


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
    """Distinct applications where FMS_TYPE='F' and Live matches the active DB."""
    # Fetch only F-type configuration rows. This is the critical dashboard
    # source now that the dashboard FMS type has changed from D to F.
    dashboard_rows = await get_fms_config(fms_type="F")
    all_rows = dashboard_rows
    options = [
        opt for opt in _group_by_app(dashboard_rows)
        if str(opt.get("table_name") or "").strip()
    ]
    options = await filter_fms_options_for_active_db(options)
    options = sorted(options, key=lambda x: str(x.get("fms_name") or "").lower())

    distinct_names = ", ".join([str(opt.get("fms_name") or "") for opt in options])
    logger.info("Loaded %d rows from FMS_Config", len(all_rows))
    logger.info("After FMS_TYPE='F': %d rows", len(dashboard_rows))
    logger.info("Distinct FMS (%s): %s", get_effective_database_target(), distinct_names or "(none)")
    logger.info("Dropdown Count: %d", len(options))
    logger.info("Final dropdown list: %s", [str(opt.get("fms_name") or "") for opt in options])

    return options


async def get_all_fms_options() -> list[dict[str, Any]]:
    """Every distinct application registered in FMS_Config filtered by active DB Live status."""
    config_rows = await get_fms_config()
    options = _group_by_app(config_rows)
    return await filter_fms_options_for_active_db(options)


async def get_table_metadata(table_name: str) -> list[dict[str, Any]]:
    return await metadata_api.get_metadata(table_name)


async def get_dashboard_filter_columns(table_name: str) -> list[dict[str, str]]:
    key = ("filter_columns", _cache_scope(), table_name.strip().lower())
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
    key = ("primary_key", _cache_scope(), table_name.strip().lower())
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
