"""
Metadata access - column definitions for any application table.

Used to dynamically generate the read-only Application Form and the
Stage Form input controls, without ever hardcoding a single field name.
"""
from __future__ import annotations

from typing import Any

from app.api.client import api_client
from app.api.table_api import as_list, get_table


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

def _parse_bool(val: Any, default: bool = False) -> bool:
    """Safely parse boolean values coming from APIs or Databases."""
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "t", "y")


async def get_metadata(table_name: str) -> list[dict[str, Any]]:
    """
    Return column metadata for `table_name`, normalized to:
    [{"name": str, "data_type": str, "nullable": bool}, ...]
    """
    raw = await api_client.get(
        "get_metadata", 
        query_params={"action": "metadata", "table": table_name}
    )
    columns = [_normalize_column(col) for col in as_list(raw)]

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
                
                # Permissions (safely parsed)
                col["is_readonly"] = _parse_bool(_get_ci(s, "IS_READONLY", "is_readonly"), False)
                
                is_ed = _get_ci(s, "IS_EDITABLE", "is_editable")
                # Editable defaults to True if missing/null
                col["is_editable"] = True if is_ed is None else str(is_ed).strip().lower() not in ("0", "false", "no", "f", "n")
                
                # Display Rules
                col["publish_name"] = _get_ci(s, "PUBLISH_NAME", "publish_name") or ""
                col["is_hidden"] = _parse_bool(_get_ci(s, "IS_HIDDEN", "is_hidden"), False)
                col["is_mandatory"] = _parse_bool(_get_ci(s, "IS_MANDATORY", "is_mandatory"), False)

                # Dropdown & Form Logic Rules
                col["dd_depends_on"] = _get_ci(s, "DD_DEPENDS_ON", "dd_depends_on") or ""
                col["dd_filter_column"] = _get_ci(s, "DD_FILTER_COLUMN", "dd_filter_column") or ""
                col["show_if_column"] = _get_ci(s, "SHOW_IF_COLUMN", "show_if_column") or ""
                col["show_if_operator"] = _get_ci(s, "SHOW_IF_OPERATOR", "show_if_operator") or ""
                col["show_if_value"] = _get_ci(s, "SHOW_IF_VALUE", "show_if_value") or ""

                # Advanced Form Settings
                col["text_rows"] = _get_ci(s, "TEXT_ROWS", "text_rows") or 2
                col["select_mode"] = _get_ci(s, "SELECT_MODE", "select_mode") or ""
                col["select_list_values"] = _get_ci(s, "SELECT_LIST_VALUES", "select_list_values") or ""
                col["select_table"] = _get_ci(s, "SELECT_TABLE", "select_table") or ""
                col["select_column"] = _get_ci(s, "SELECT_COLUMN", "select_column") or ""
                col["display_order"] = _to_int(_get_ci(s, "DISPLAY_ORDER", "display_order"), 0)
                
    except Exception as e:
        print(f"Failed to load form settings for {table_name}: {e}")

    # Ensure display order logic functions seamlessly
    for index, col in enumerate(columns, start=1):
        if not col.get("display_order"):
            col["display_order"] = index

    columns = sorted(
        enumerate(columns),
        key=lambda item: (_to_int(item[1].get("display_order"), item[0] + 1), item[0]),
    )
    columns = [col for _, col in columns]

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
    nullable = _parse_bool(nullable_raw, True) if nullable_raw is not None else True
    
    max_length = _get_ci(col, "character_maximum_length", "max_length")
    is_primary_key = bool(_get_ci(col, "is_primary_key", "primary_key")) or False

    return {
        "name": name,
        "data_type": str(data_type).lower(),
        "nullable": nullable,
        "max_length": max_length,
        "is_primary_key": is_primary_key,
    }


async def save_column_settings(table_name: str, settings_payload: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Send updated column input types and permissions to the upstream API."""
    payload = {
        "action": "save_settings",
        "table": table_name,
        "columns": settings_payload
    }
    return await api_client.post(
        "save_form_settings", 
        query_params={"action": "save_settings", "table": table_name},
        json_body=payload
    )