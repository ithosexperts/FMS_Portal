"""
Generic metadata-driven table access - works for ANY table name, including
the configuration tables (TM_Application_Master, FMS_Config) and every
application data table (TM_Hose_Assy, TM_Production, ...).

No table name is ever hardcoded here - callers pass it in.
"""
from __future__ import annotations

from typing import Any

from app.api.client import api_client
from app.api.client import ApiError

WRAPPER_KEYS = {
    "data",
    "rows",
    "items",
    "result",
    "results",
    "records",
    "recordset",
    "value",
    "payload",
    "list",
}


def _is_wrapper_dict(value: Any) -> bool:
    return isinstance(value, dict) and any(k in WRAPPER_KEYS for k in value.keys())


async def get_table(table_name: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return all rows of `table_name`, optionally filtered."""
    query_params: dict[str, Any] = {
        "action": "select",
        "table": table_name,
        "fetchAll": "true",
    }
    if filters:
        query_params.update(filters)
    data = await api_client.get("table_gateway", query_params=query_params)
    return as_list(data)


async def get_row(table_name: str, row_id: Any) -> dict[str, Any] | None:
    """
    Return a single row of `table_name` identified by its primary key.
    Since the underlying API_1 'get_row' path is empty, we proxy this through 
    get_table (table_gateway) and filter exactly to ensure data maps correctly.
    """
    from app.services.metadata_service import resolve_primary_key
    
    try:
        pk = await resolve_primary_key(table_name)
        # 1. Attempt to fetch with the PK as a filter
        rows = await get_table(table_name, filters={pk: row_id})
        
        target = str(row_id).strip().lower()
        
        # Check exact PK match in the returned rows
        for row in rows:
            if str(row.get(pk, "")).strip().lower() == target:
                return row
                
        # 2. If API ignored the filter (returned everything) or returned empty, 
        # fetch all without filter as a fallback measure
        if len(rows) <= 1:
            rows = await get_table(table_name)
            
        for row in rows:
            if str(row.get(pk, "")).strip().lower() == target:
                return row
                
        # 3. Final fallback: scan any column for the row_id
        for row in rows:
            if target in [str(v).strip().lower() for v in row.values() if v is not None]:
                return row
    except Exception:
        # If resolve_primary_key fails, fallback to simple get_table scan
        rows = await get_table(table_name)
        target = str(row_id).strip().lower()
        for row in rows:
            if target in [str(v).strip().lower() for v in row.values() if v is not None]:
                return row
                
    return None


async def update_row(table_name: str, row_id: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Persist changes to a single row (used by stage form Save/Submit)."""
    from app.services.metadata_service import resolve_primary_key
    pk = await resolve_primary_key(table_name)
    
    query_params = {
        "action": "update",
        "table": table_name
    }
    
    # Use the explicit data and where structure to bypass SQL PK checks 
    # for parent tables like Z_SalesOrder
    update_payload = {
        "action": "update",
        "table": table_name,
        "data": payload,
        "where": {
            pk: row_id
        }
    }

    data = await api_client.post(
        "update_row",
        query_params=query_params,
        json_body=update_payload,
    )
    return as_dict(data)


async def insert_row(table_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Insert a new row into `table_name` using the gateway action contract."""
    insert_payload = {
        "action": "insert",
        "table": table_name,
        "data": payload,
        "skip_primary_key_check": True,
        "validate_primary_key": False,
    }
    data = await api_client.post(
        "update_row",
        json_body=insert_payload,
    )
    result = as_dict(data)
    if isinstance(result, dict):
        message = str(result.get("message") or result.get("error") or "").strip()
        sql_state = str(result.get("SQLSTATE") or result.get("sqlstate") or "").strip()
        code = result.get("code")
        has_error_shape = bool(message) or bool(sql_state) or code not in (None, "", 0, "0")
        if has_error_shape:
            raise ApiError(message or f"Insert failed for {table_name}")
    return result


async def search(table_name: str, query: str, columns: list[str] | None = None) -> list[dict[str, Any]]:
    """Free-text search across `table_name` (dashboard search box)."""
    params: dict[str, Any] = {"q": query}
    if columns:
        params["columns"] = ",".join(columns)
    data = await api_client.get("search_table", path_params={"table_name": table_name}, query_params=params)
    return as_list(data)


def as_list(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        if not data:
            return []

        # Some APIs return wrappers inside lists, e.g. [{"data": [...] }].
        flattened: list[dict[str, Any]] = []
        wrapper_detected = False
        for item in data:
            if _is_wrapper_dict(item):
                wrapper_detected = True
                flattened.extend(as_list(item))
            elif isinstance(item, list):
                wrapper_detected = True
                flattened.extend(as_list(item))
            elif isinstance(item, dict):
                flattened.append(item)

        if wrapper_detected:
            return flattened
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in WRAPPER_KEYS:
            if isinstance(data.get(key), list):
                return as_list(data[key])
        # Some APIs nest the actual list one level deeper (e.g. payload/result).
        for value in data.values():
            nested = as_list(value)
            if nested:
                return nested
    return []


def as_dict(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if isinstance(data, dict):
        for key in ("data", "row", "result", "record", "payload"):
            if isinstance(data.get(key), dict):
                return data[key]
        for value in data.values():
            nested = as_dict(value)
            if nested:
                return nested
        return data
    if isinstance(data, list) and data:
        return data[0]
    return None