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

    def _get_ci(row_dict: dict, col_name: str) -> Any:
        col_lower = str(col_name).strip().lower()
        for k, v in row_dict.items():
            if str(k).strip().lower() == col_lower:
                return v
        return None

    target = str(row_id).strip().lower()
    
    try:
        pk = await resolve_primary_key(table_name)
        # 1. Attempt to fetch with the PK as a filter
        rows = await get_table(table_name, filters={pk: row_id})
        
        # Check case-insensitive PK match in the returned rows
        for row in rows:
            val = _get_ci(row, pk)
            if val is not None and str(val).strip().lower() == target:
                return row

        # Check if any column in returned filtered rows matches target
        for row in rows:
            for v in row.values():
                if v is not None and str(v).strip().lower() == target:
                    return row

        # If primary key filter returned nothing, try other common PK filter names
        if not rows:
            for alt_pk in ("RowID", "ID", "PK", "TaskID", "DocNum"):
                if alt_pk.lower() != pk.lower():
                    alt_rows = await get_table(table_name, filters={alt_pk: row_id})
                    for row in alt_rows:
                        val = _get_ci(row, alt_pk)
                        if val is not None and str(val).strip().lower() == target:
                            return row
                        for v in row.values():
                            if v is not None and str(v).strip().lower() == target:
                                return row
                    if alt_rows:
                        break
    except Exception:
        pass

    # Fallback to simple get_table scan only if targeted queries fail
    try:
        rows = await get_table(table_name)
        for row in rows:
            for v in row.values():
                if v is not None and str(v).strip().lower() == target:
                    return row
    except Exception:
        pass

    return {}
                
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
        # The Hosexperts table gateway returns a successful insert as
        # {"message": "Record inserted"}. Treating every non-empty message
        # as an error caused the database insert to succeed while Runner360
        # displayed a failure to the user. Only explicit error indicators or
        # unrecognized failure messages should raise ApiError.
        message = str(result.get("message") or "").strip()
        error_value = str(result.get("error") or "").strip()
        sql_state = str(result.get("SQLSTATE") or result.get("sqlstate") or "").strip()
        code = result.get("code")
        status = result.get("status")

        normalized_message = " ".join(message.casefold().replace("_", " ").split())
        success_messages = {
            "record inserted",
            "record inserted successfully",
            "inserted",
            "inserted successfully",
            "insert successful",
            "insert success",
            "success",
            "successful",
            "ok",
        }
        is_success_message = (
            normalized_message in success_messages
            or normalized_message.startswith("record inserted ")
            or normalized_message.startswith("successfully inserted")
        )

        status_is_failure = False
        try:
            if status not in (None, "") and int(status) >= 400:
                status_is_failure = True
        except (TypeError, ValueError):
            status_is_failure = str(status).strip().casefold() in {
                "error", "failed", "failure", "false"
            }

        code_is_failure = code not in (None, "", 0, "0")
        if error_value or sql_state or status_is_failure or code_is_failure:
            raise ApiError(
                error_value
                or message
                or sql_state
                or f"Insert failed for {table_name}"
            )

        if message and not is_success_message:
            # Some gateways return HTTP 200 for SQL/application failures.
            raise ApiError(message)
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