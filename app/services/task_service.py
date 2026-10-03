"""Fast task/context resolution shared by dashboard details and every form."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.api import table_api
from app.services import metadata_service
from app.utils.stage_calculator import get_current_stage, sorted_stages
from app.utils.status_calculator import build_timeline, calculate_task_status


class TaskNotFoundError(Exception):
    """Raised when a Task ID cannot be located."""


_LOCATOR_TTL = 60.0
_locator_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_locator_locks: dict[str, asyncio.Lock] = {}


def _lock(task_id: str) -> asyncio.Lock:
    return _locator_locks.setdefault(task_id, asyncio.Lock())


async def _locate_task(
    task_id: str,
    table_name: str | None = None,
    app_id: str | None = None,
    primary_key: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Locate a task with a direct table lookup whenever the caller knows it.

    Dashboard rows already know their FMS/table. Older deep links may only have
    the task id, so the fallback probes registered tables concurrently rather
    than waiting for every table one-by-one.
    """
    normalized_id = str(task_id).strip()
    direct_table = str(table_name or "").strip()
    direct_app = str(app_id or "").strip()

    if direct_table:
        pk = str(primary_key or "").strip() or await metadata_service.resolve_primary_key(direct_table)
        rows = await table_api.get_table(direct_table, filters={pk: normalized_id})
        row = next((r for r in rows if str(r.get(pk, "")).strip().lower() == normalized_id.lower()), None)
        if row is None and rows and len(rows) == 1:
            row = rows[0]
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' was not found in table '{direct_table}'.")
        config_rows = await metadata_service.get_fms_config(app_id=direct_app) if direct_app else await metadata_service.get_fms_config()
        matching = [r for r in config_rows if str(r.get("TABLE_NAME") or "").strip().lower() == direct_table.lower()]
        if direct_app:
            matching = [r for r in matching if str(r.get("APP_ID") or "").strip() == direct_app]
        if not matching:
            raise TaskNotFoundError(f"No FMS configuration found for table '{direct_table}'.")
        fms = metadata_service._group_by_app(matching)[0]
        return fms, matching, row

    cached = _locator_cache.get(normalized_id.lower())
    if cached and time.monotonic() - cached[0] < _LOCATOR_TTL:
        item = cached[1]
        fms = item["fms"]
        pk = await metadata_service.resolve_primary_key(fms["table_name"])
        rows = await table_api.get_table(fms["table_name"], filters={pk: normalized_id})
        row = next((r for r in rows if str(r.get(pk, "")).strip().lower() == normalized_id.lower()), None)
        if row is not None:
            return fms, item["stage_configs"], row

    async with _lock(normalized_id.lower()):
        cached = _locator_cache.get(normalized_id.lower())
        if cached and time.monotonic() - cached[0] < _LOCATOR_TTL:
            item = cached[1]
            fms = item["fms"]
            pk = await metadata_service.resolve_primary_key(fms["table_name"])
            rows = await table_api.get_table(fms["table_name"], filters={pk: normalized_id})
            row = next((r for r in rows if str(r.get(pk, "")).strip().lower() == normalized_id.lower()), None)
            if row is not None:
                return fms, item["stage_configs"], row

        config_rows = await metadata_service.get_fms_config()
        fms_options = metadata_service._group_by_app(config_rows)
        config_by_app: dict[str, list[dict[str, Any]]] = {}
        for row in config_rows:
            config_by_app.setdefault(str(row.get("APP_ID") or ""), []).append(row)

        async def probe(fms: dict[str, Any]):
            table = str(fms.get("table_name") or "").strip()
            if not table:
                return None
            try:
                pk = await metadata_service.resolve_primary_key(table)
                rows = await table_api.get_table(table, filters={pk: normalized_id})
                row = next((r for r in rows if str(r.get(pk, "")).strip().lower() == normalized_id.lower()), None)
                if row is not None:
                    return fms, config_by_app.get(str(fms.get("app_id") or ""), []), row
            except Exception:
                return None
            return None

        results = await asyncio.gather(*(probe(f) for f in fms_options))
        for result in results:
            if result:
                fms, stage_configs, row = result
                _locator_cache[normalized_id.lower()] = (time.monotonic(), {"fms": fms, "stage_configs": stage_configs})
                return fms, stage_configs, row

    raise TaskNotFoundError(f"Task '{task_id}' was not found in any registered application table.")


async def get_task_context(
    task_id: str,
    table_name: str | None = None,
    app_id: str | None = None,
    primary_key: str | None = None,
) -> dict[str, Any]:
    """Single source of truth consumed by Task Details / Timeline / Forms pages."""
    fms, stage_configs, row = await _locate_task(task_id, table_name=table_name, app_id=app_id, primary_key=primary_key)
    stages = sorted_stages(stage_configs)
    primary_key = await metadata_service.resolve_primary_key(fms["table_name"])
    return {
        "task_id": task_id,
        "fms": fms,
        "stage_configs": stages,
        "row": row,
        "timeline": build_timeline(stage_configs, row),
        "current_stage": get_current_stage(stage_configs, row),
        "status": calculate_task_status(stage_configs, row),
        "primary_key": primary_key,
    }
