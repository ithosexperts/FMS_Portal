
"""
Metadata access - column definitions for any application table.

Used to dynamically generate the read-only Application Form and the
Stage Form input controls, without ever hardcoding a single field name.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.api.client import api_client
from app.api.table_api import as_list, get_table
from app.utils.input_mapper import map_input_type

_METADATA_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_METADATA_LOCKS: dict[str, asyncio.Lock] = {}
_METADATA_TTL = 60.0


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _is_true(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y")


async def get_metadata(table_name: str) -> list[dict[str, Any]]:
    """
    Return column metadata for `table_name`, normalized to:
    [{"name": str, "data_type": str, "nullable": bool}, ...]
    """
    key = str(table_name or "").strip().lower()
    now = time.monotonic()
    cached = _METADATA_CACHE.get(key)
    if cached and now - cached[0] < _METADATA_TTL:
        return cached[1]
    lock = _METADATA_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _METADATA_CACHE.get(key)
        if cached and now - cached[0] < _METADATA_TTL:
            return cached[1]
        raw = await api_client.get(
            "get_metadata",
            query_params={"action": "metadata", "table": table_name},
        )
        columns = [_normalize_column(col) for col in as_list(raw)]

        # Fetch previously saved form settings from TM_Form_Settings if present.
        try:
            saved_rows = await get_table("TM_Form_Settings", filters={"TABLE_NAME": table_name})
            settings_map = {}
            for row in saved_rows:
                column_name = _get_ci(row, "COLUMN_NAME", "column_name")
                if column_name:
                    settings_map[str(column_name).strip().lower()] = row

            for col in columns:
                key = str(col.get("name", "")).lower()
                if key in settings_map:
                    s = settings_map[key]
                    input_type = _get_ci(s, "INPUT_TYPE", "input_type")
                    if input_type:
                        col["input_type"] = input_type

                    is_readonly = _get_ci(s, "IS_READONLY", "is_readonly")
                    is_editable = _get_ci(s, "IS_EDITABLE", "is_editable")
                    col["is_readonly"] = str(is_readonly).strip() in ("1", "true", "yes")
                    col["is_editable"] = True if is_editable is None else str(is_editable).strip() not in ("0", "false", "no")

                    col["publish_name"] = _get_ci(s, "PUBLISH_NAME", "publish_name") or ""
                    col["apply_filter"] = _is_true(_get_ci(s, "APPLY_FILTER", "Apply_Filter", "apply_filter"))

                    is_hidden = _get_ci(s, "IS_HIDDEN", "is_hidden")
                    col["is_hidden"] = str(is_hidden).strip() in ("1", "true", "yes")

                    is_mandatory = _get_ci(s, "IS_MANDATORY", "is_mandatory")
                    col["is_mandatory"] = str(is_mandatory).strip() in ("1", "true", "yes")

                    col["dd_depends_on"] = _get_ci(s, "DD_DEPENDS_ON", "dd_depends_on") or ""
                    col["dd_filter_column"] = _get_ci(s, "DD_FILTER_COLUMN", "dd_filter_column") or ""
                    col["show_if_column"] = _get_ci(s, "SHOW_IF_COLUMN", "show_if_column") or ""
                    col["show_if_operator"] = _get_ci(s, "SHOW_IF_OPERATOR", "show_if_operator") or ""
                    col["show_if_value"] = _get_ci(s, "SHOW_IF_VALUE", "show_if_value") or ""

                    col["text_rows"] = _get_ci(s, "TEXT_ROWS", "text_rows") or 2
                    col["select_mode"] = _get_ci(s, "SELECT_MODE", "select_mode") or ""
                    col["select_list_values"] = _get_ci(s, "SELECT_LIST_VALUES", "select_list_values") or ""
                    col["select_table"] = _get_ci(s, "SELECT_TABLE", "select_table") or ""
                    col["select_column"] = _get_ci(s, "SELECT_COLUMN", "select_column") or ""
                    col["display_order"] = _to_int(_get_ci(s, "DISPLAY_ORDER", "display_order"), 0)
        except Exception as e:
            print(f"Failed to load form settings for {table_name}: {e}")

        # Fallback to intelligent input mapping if no saved setting existed.
        for col in columns:
            if not col.get("input_type"):
                col["input_type"] = map_input_type(col["name"], col["data_type"])

        # Ensure display order logic functions seamlessly.
        for index, col in enumerate(columns, start=1):
            if not col.get("display_order"):
                col["display_order"] = index

        columns = sorted(
            enumerate(columns),
            key=lambda item: (_to_int(item[1].get("display_order"), item[0] + 1), item[0]),
        )
        columns = [col for _, col in columns]

        _METADATA_CACHE[key] = (time.monotonic(), columns)
        return columns


def _get_ci(row: dict[str, Any], *keys: str) -> Any:
    """Case-insensitive dict lookup - upstream key casing is not guaranteed."""
    lowered = {k.lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def _normalize_column(col: dict[str, Any]) -> dict[str, Any]:
    name = _get_ci(col, "name", "column_name")
    data_type = _get_ci(col, "data_type", "type", "sql_type") or "varchar"
    nullable_raw = _get_ci(col, "nullable", "is_nullable")
    nullable = str(nullable_raw).strip().upper() in ("YES", "TRUE", "1") if nullable_raw is not None else True
    max_length = _get_ci(col, "character_maximum_length", "max_length")
    is_primary_key = bool(_get_ci(col, "is_primary_key", "primary_key")) or False

    return {
        "name": name,
        "data_type": str(data_type).lower(),
        "nullable": nullable,
        "max_length": max_length,
        "is_primary_key": is_primary_key,
        "input_type": None,
    }


async def save_column_settings(table_name: str, settings_payload: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Send updated column input types and permissions to the upstream API."""
    payload = {
        "action": "save_settings",
        "table": table_name,
        "columns": settings_payload,
    }
    return await api_client.post(
        "save_form_settings",
        query_params={"action": "save_settings", "table": table_name},
        json_body=payload,
    )


async def save_apply_filter_flags(table_name: str, flags_by_column: dict[str, str]) -> None:
    """Best-effort fallback to persist Apply_Filter directly in TM_Form_Settings."""
    for column_name, value in flags_by_column.items():
        if not column_name:
            continue
        flag = "Y" if str(value).strip().upper() == "Y" else "N"
        payload = {
            "action": "update",
            "table": "TM_Form_Settings",
            "data": {
                "Apply_Filter": flag,
            },
            "where": {
                "TABLE_NAME": table_name,
                "COLUMN_NAME": column_name,
            },
        }
        try:
            await api_client.post(
                "update_row",
                query_params={"action": "update", "table": "TM_Form_Settings"},
                json_body=payload,
            )
        except Exception:
            continue



