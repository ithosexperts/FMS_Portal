"""
Pick / Pack custom Stage-1 service for the Pack to Dispatch FMS.

This module intentionally contains Python only.  The browser-side implementation
lives in app/static/js/stage1-custom-form.js.

The service is metadata tolerant because the SQL gateway may return slightly
different column casing/naming between environments.
"""
from __future__ import annotations

import os
import time
from typing import Any

from app.api import table_api
from app.utils.delay_calculator import is_blank


PICKER_MASTER_TABLE = os.getenv("PICKER_MASTER_TABLE", "FL_Users").strip()
BATCH_TABLE = os.getenv("PICK_N_PACK_BATCH_TABLE", "Z_SOPickNpackBatchDetails").strip()
SALES_ORDER_TABLE = "Z_SalesOrder"
PICKER_LOCATION_TEAM_MAP = {"delhi": "29", "noida": "30"}

_CACHE_TTL_SECONDS = 5.0
_sales_order_cache: tuple[float, list[dict[str, Any]]] | None = None
_picker_options_cache: tuple[float, list[dict[str, str]]] | None = None


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _text(value: Any) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.casefold() in {"none", "null"} else value


def _value(row: dict[str, Any], *names: str, suffix: bool = True) -> Any:
    if not isinstance(row, dict):
        return None
    targets = {_norm(n) for n in names if n}
    for key, value in row.items():
        if _norm(key) in targets:
            return value
    if suffix:
        for key, value in row.items():
            key_norm = _norm(key)
            if any(key_norm.endswith(target) for target in targets):
                return value
    return None


def _row_id(row: dict[str, Any]) -> str:
    return _text(_value(row, "RowID", "ID", "PK", "row_id"))


def _pick_id(row: dict[str, Any]) -> str:
    return _text(_value(row, "S1_PickID", "PickID", "S1PickID"))


def _parse_list(value: Any, limit: int = 5) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").split(",")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        item = _text(item)
        key = _norm(item)
        if item and key and key not in seen:
            seen.add(key)
            result.append(item)
        if len(result) >= limit:
            break
    return result


def invalidate_picker_assignment_cache() -> None:
    global _sales_order_cache, _picker_options_cache
    _sales_order_cache = None
    _picker_options_cache = None


async def get_sales_order_picker_rows(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return the complete Z_SalesOrder source used by picker availability.

    Busy Picker is a cross-row operation, so it should not make 100+ browser/API
    calls just to build the board.  The generic table gateway already supports
    ``fetchAll=true``; use that as the primary path and only fall back to the
    dashboard's 50-row pagination when the all-row request fails.
    """
    global _sales_order_cache
    now = time.monotonic()
    if (
        not force_refresh
        and _sales_order_cache
        and now - _sales_order_cache[0] < _CACHE_TTL_SECONDS
    ):
        return _sales_order_cache[1]

    rows: list[dict[str, Any]] = []

    # Primary path: one request.  This avoids the previous implementation
    # creating 118+ concurrent requests for a table that the gateway can return
    # with fetchAll=true.
    try:
        direct_rows = await table_api.get_table(SALES_ORDER_TABLE)
        if isinstance(direct_rows, list):
            rows = [row for row in direct_rows if isinstance(row, dict)]
            print(f"[S1 PICKER] Loaded {len(rows)} {SALES_ORDER_TABLE} rows via fetchAll")
    except Exception as exc:
        print(f"[S1 PICKER] fetchAll read failed for {SALES_ORDER_TABLE}: {exc}")

    # Fallback: use the dashboard reader, but keep concurrency deliberately
    # small so the PHP gateway is not flooded when the table has many pages.
    if not rows:
        try:
            from app.services.dashboard_service import _fetch_dashboard_api_page
            import asyncio

            page_size = 50
            first_rows, total_pages, _ = await _fetch_dashboard_api_page(
                SALES_ORDER_TABLE, filters=None, page=1, page_size=page_size
            )
            rows.extend(first_rows or [])
            total_pages = max(int(total_pages or 1), 1)

            if total_pages > 1:
                semaphore = asyncio.Semaphore(3)

                async def fetch_page(page_no: int) -> list[dict[str, Any]]:
                    async with semaphore:
                        page_rows, _total, _current = await _fetch_dashboard_api_page(
                            SALES_ORDER_TABLE,
                            filters=None,
                            page=page_no,
                            page_size=page_size,
                        )
                        return page_rows or []

                remaining = await asyncio.gather(
                    *(fetch_page(page_no) for page_no in range(2, total_pages + 1)),
                    return_exceptions=True,
                )
                for result in remaining:
                    if isinstance(result, Exception):
                        print(f"[S1 PICKER] Sales-order page read failed: {result}")
                        continue
                    rows.extend(result)
        except Exception as exc:
            print(f"[S1 PICKER] Paginated read failed for {SALES_ORDER_TABLE}: {exc}")

    # If both routes failed, preserve the old generic table fallback.  Do not
    # hide the real failure in the logs; the caller still receives an empty
    # board rather than an HTML/500 response.
    if not rows:
        try:
            rows = await table_api.get_table(SALES_ORDER_TABLE)
        except Exception as fallback_exc:
            print(f"[S1 PICKER] Final read failed for {SALES_ORDER_TABLE}: {fallback_exc}")
            rows = []

    # De-duplicate physical rows when a gateway returns overlapping/fallback data.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rid = _row_id(row)
        key = f"row:{_norm(rid)}" if rid else f"obj:{id(row)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    _sales_order_cache = (now, unique)
    print(f"[S1 PICKER] Busy Picker source contains {len(unique)} unique {SALES_ORDER_TABLE} rows")
    return unique


def _picker_name(row: dict[str, Any]) -> str:
    for name in (
        "PickerName", "Picker_Name", "Name", "FullName", "EmployeeName",
        "Employee_Name", "UserName", "Username", "DisplayName"
    ):
        value = _text(_value(row, name, suffix=False))
        if value:
            return value
    for key, value in row.items():
        nk = _norm(key)
        if "name" in nk and "user" not in nk:
            value = _text(value)
            if value:
                return value
    return ""


def _picker_role(row: dict[str, Any]) -> str:
    for name in ("Role", "UserRole", "RoleName", "User_Role", "UserType", "Designation"):
        value = _text(_value(row, name, suffix=False))
        if value:
            return value
    return ""


async def _load_picker_options(location: Any) -> list[dict[str, str]]:
    """Load picker employees from the location-specific team master.

    The Stage-1 picker list is driven by Location -> VM_Team.TeamID ->
    VM_EmployeeMaster.EmployeeCode/EmployeeName.  This is the same master
    relationship used by the working Pick/Pack form, and avoids depending on
    FL_Users role text being populated as "PICKER".
    """
    location_key = _norm(location)
    target_team_id = PICKER_LOCATION_TEAM_MAP.get(location_key)

    employee_rows: list[dict[str, Any]] = []
    team_rows: list[dict[str, Any]] = []
    try:
        employee_rows = await table_api.get_table("VM_EmployeeMaster")
        team_rows = await table_api.get_table("VM_Team")
    except Exception as exc:
        print(f"[S1 PICKER] Failed to load picker masters: {exc}")

    def field_value(row: dict[str, Any], *names: str) -> Any:
        targets = {_norm(name) for name in names}
        for key, value in (row or {}).items():
            if _norm(key) in targets:
                return value
        return None

    employee_by_code: dict[str, str] = {}
    for employee_row in employee_rows or []:
        code = _text(field_value(
            employee_row,
            "EmployeeCode", "EMPLOYEECODE", "employee_code", "Employee Code"
        ))
        name = _text(field_value(
            employee_row,
            "EmployeeName", "EMPLOYEENAME", "Employee_Name", "Name"
        ))
        if code and name:
            employee_by_code[_norm(code)] = name

    names: list[str] = []
    seen: set[str] = set()
    if target_team_id:
        for team_row in team_rows or []:
            team_id = _text(field_value(
                team_row,
                "TeamID", "TeamId", "TEAM_ID", "Team_ID", "team_id"
            ))
            if team_id != target_team_id:
                continue
            employee_code = _text(field_value(
                team_row,
                "EmployeeCode", "EMPLOYEECODE", "employee_code", "Employee Code"
            ))
            employee_name = employee_by_code.get(_norm(employee_code), "")
            if not employee_name:
                continue
            key = _norm(employee_name)
            if key in seen:
                continue
            seen.add(key)
            names.append(employee_name)

    # Keep a safe fallback for installations where the VM team masters are not
    # available.  This fallback is used only when the location/team mapping
    # produced no names; it preserves compatibility with a dedicated FL_Users
    # picker master without changing the normal location-based behavior.
    if not names and PICKER_MASTER_TABLE:
        try:
            fallback_rows = await table_api.get_table(PICKER_MASTER_TABLE)
        except Exception as exc:
            print(f"[S1 PICKER] Fallback picker master read failed: {exc}")
            fallback_rows = []
        for row in fallback_rows or []:
            name = _picker_name(row)
            if not name:
                continue
            role = _picker_role(row)
            if role and "picker" not in _norm(role):
                continue
            key = _norm(name)
            if key in seen:
                continue
            seen.add(key)
            names.append(name)

    names.sort(key=lambda value: value.casefold())
    return [{"name": name, "label": name} for name in names]


def _empty_field(field: str, caption: str, value: Any = None) -> dict[str, Any]:
    return {"field": field, "caption": caption, "value": value}


def _field_from_row(row: dict[str, Any], field: str, caption: str, *aliases: str) -> dict[str, Any]:
    return _empty_field(field, caption, _value(row, field, *aliases))


async def _load_publish_names(table_name: str) -> dict[str, str]:
    """Load TM_Form_Settings Publish Name values for custom-form labels."""
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
        print(f"[S1 Publish Name] Failed to load TM_Form_Settings for {table_name}: {exc}")
        return {}

    out: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        field_name = None
        publish_name = None
        for key, value in row.items():
            key_norm = norm(key)
            if key_norm in {"field", "fieldname", "column", "columnname", "dbfield", "dbname", "name"} and field_name is None:
                field_name = value
            elif key_norm in {"publishname", "publish", "displayname", "caption", "label"} and publish_name is None:
                publish_name = value
        if field_name is None or publish_name is None:
            continue
        field_key = norm(field_name)
        publish_text = str(publish_name).strip()
        if not field_key or not publish_text or publish_text.lower() in {"default name...", "default name", "null", "none"}:
            continue
        out[field_key] = publish_text
    return out


def _publish_label(publish_names: dict[str, str], field_name: str, fallback: str) -> str:
    key = "".join(ch for ch in str(field_name or "").lower() if ch.isalnum())
    return publish_names.get(key) or fallback


def _clean(value: Any) -> Any:
    return None if _text(value) == "" else value


def _is_complete(value: Any) -> bool:
    return _norm(value) in {
        "done", "donedelay", "completed", "complete", "closed", "closedelay"
    }


def _line_number(row: dict[str, Any]) -> str:
    return _text(_value(
        row, "I_LineNum", "LineNum", "LineNumber", "Line No", "LineNo",
        "SO_LineNum", "S1_LineNum"
    ))


def _batch_line_number(row: dict[str, Any]) -> str:
    return _text(_value(
        row, "I_LineNum", "LineNum", "LineNumber", "Line No", "LineNo",
        "Line_Num", "S1_LineNum"
    ))


def _batch_key(row: dict[str, Any]) -> str:
    return _text(_value(row, "BatchKey", "Batch_Key", "BatchNo", "BatchNum", "BatchNumber"))


def _batch_rows_for(order_no: str, create_ts: str) -> list[dict[str, Any]]:
    # placeholder kept synchronous for grouping helper; actual fetch happens below
    return []


async def _load_batch_rows(order_no: str, create_ts: str) -> list[dict[str, Any]]:
    """Load batch-detail rows for the current Sales Order.

    I_CreateTS belongs to Z_SalesOrder and is used only to identify the S1
    group.  Z_SOPickNpackBatchDetails is joined by DocNum/LineNum/ItemCode.
    Do not require I_CreateTS/CreateTS on the batch table.
    """
    if not BATCH_TABLE or not order_no:
        return []

    candidates: list[dict[str, Any]] = []

    # First use the real batch-table key visible in SQL: DocNum.
    for flt in (
        {"DocNum": order_no},
        {"I_DocNum": order_no},
    ):
        try:
            rows = await table_api.get_table(BATCH_TABLE, filters=flt)
        except Exception as exc:
            print(f"[S1 Batch] lookup failed filters={flt}: {exc}")
            rows = []
        if rows:
            candidates.extend(row for row in rows if isinstance(row, dict))

    # Some gateway builds ignore/strip the filter on this custom table.  In
    # that case fetch the batch table once and filter DocNum locally.  This is
    # the important fallback that prevents a valid Allocated row from being
    # reported as missing simply because the gateway ignored a filter alias.
    if not candidates:
        try:
            all_rows = await table_api.get_table(BATCH_TABLE)
            wanted_doc = _text(order_no).casefold()
            candidates = [
                row for row in (all_rows or [])
                if isinstance(row, dict)
                and _text(_value(row, "DocNum", "I_DocNum", "SalesOrderNo")).casefold() == wanted_doc
            ]
            print(
                f"[S1 Batch] filter fallback: scanned {len(all_rows or [])} "
                f"batch rows and found {len(candidates)} for DocNum={order_no}"
            )
        except Exception as exc:
            print(f"[S1 Batch] all-row fallback failed: {exc}")

    # De-duplicate physical batch rows returned by both DocNum aliases.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        rid = _row_id(row)
        key = "row:" + _norm(rid) if rid else "obj:" + repr(sorted((str(k), str(v)) for k, v in row.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    print(
        f"[S1 Batch] Loaded {len(unique)} batch rows for DocNum={order_no}; "
        f"group CreateTS={create_ts or '-'}"
    )
    return unique


def _actual_timestamp_value(row: dict[str, Any]) -> Any:
    # S1 actual/completion timestamp is the switch that determines which
    # physical batch allocation should be displayed in the form.
    return _value(
        row,
        "S1_ActualTimestamp",
        "S1_ALTimeStamp",
        "S1_ALTime",
        "S1_ActualDateTime",
    )


def _normalize_batch_status(value: Any) -> str:
    return "".join(ch for ch in _text(value).casefold() if ch.isalnum())


def _normalize_join_key(value: Any) -> str:
    """Normalize SQL identifiers so 0, 0.0 and whitespace compare equally."""
    text = _text(value)
    if not text:
        return ""
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return text.casefold()


async def _build_batch_groups(
    sales_rows: list[dict[str, Any]],
    order_no: str,
    create_ts: str,
) -> tuple[list[dict[str, Any]], float | None, str]:
    batch_rows = await _load_batch_rows(order_no, create_ts)

    # Before S1 is completed, the allocation table rows shown in the form
    # must be BatchStatus=Allocated. Once S1_ActualTimestamp exists, that
    # same physical allocation is expected to be represented by
    # BatchStatus=Delivered and ONLY those Delivered rows should be shown.
    stage_actual = next(
        (
            value
            for value in (_actual_timestamp_value(r) for r in sales_rows)
            if not is_blank(value)
        ),
        None,
    )
    target_batch_status = "Delivered" if not is_blank(stage_actual) else "Allocated"
    target_batch_status_norm = _normalize_batch_status(target_batch_status)

    by_line: dict[str, list[dict[str, Any]]] = {}
    wanted_doc = _normalize_join_key(order_no)
    for batch in batch_rows:
        batch_status = _normalize_batch_status(
            _value(batch, "BatchStatus", "Batch_Status", "Status")
        )
        if batch_status != target_batch_status_norm:
            continue

        batch_doc = _normalize_join_key(_value(batch, "DocNum", "I_DocNum", "SalesOrderNo"))
        if batch_doc and wanted_doc and batch_doc != wanted_doc:
            continue

        line = _normalize_join_key(_batch_line_number(batch))
        if not line:
            continue
        by_line.setdefault(line, []).append(batch)

    print(
        f"[S1 Batch] DocNum={order_no} target_status={target_batch_status}; "
        f"eligible_lines={sorted(by_line.keys())}"
    )

    groups: list[dict[str, Any]] = []
    total_weight = 0.0
    have_weight = False

    for sales_row in sales_rows:
        line = _line_number(sales_row)
        if not line:
            # Keep rows without a line number grouped separately.
            line = str(len(groups) + 1)

        weight_raw = _value(
            sales_row, "S1_LineWeight", "S1_Weight", "LineWeight", "Weight",
            "WeightKgs", "S1_WeightKgs"
        )
        try:
            weight = float(weight_raw) if _text(weight_raw) else None
        except (TypeError, ValueError):
            weight = None
        if weight is not None:
            total_weight += weight
            have_weight = True

        rows: list[dict[str, Any]] = []
        line_key = _normalize_join_key(line)
        for batch in by_line.get(line_key, []):
            submission = _value(batch, "S1_Submission", "Submission")
            remarks = _value(batch, "S1_Remarks", "Remarks")
            rows.append({
                "row_id": _row_id(batch),
                "batch_key": _batch_key(batch) or _row_id(batch),
                "batch_num": _text(_value(batch, "BatchNo", "BatchNum", "BatchNumber", "Batch")),
                "available_qty": _clean(_value(batch, "AvailableQty", "Available_Qty", "QtyAvailable", "Quantity")),
                "allocated_qty": _clean(_value(batch, "AllocatedQty", "Allocated_Qty", "QtyAllocated", "QuantityAllocated")),
                "batch_attribute_1": _clean(_value(batch, "BatchAttribute1", "Batch_Attribute1", "BatchAttr1")),
                "batch_attribute_2": _clean(_value(batch, "BatchAttribute2", "Batch_Attribute2", "BatchAttr2")),
                "packed": _norm(submission) in {"1", "yes", "true", "y", "packed"},
                "remarks": _text(remarks),
            })

        print(
            f"[S1 Batch] form line={line} item={_text(_value(sales_row, 'ItemCode', 'I_ItemCode', 'Item_Code'))} "
            f"status={target_batch_status} matched_batches={len(rows)}"
        )

        groups.append({
            "line_num": line,
            "sales_row_id": _row_id(sales_row),
            "item_code": _clean(_value(sales_row, "ItemCode", "I_ItemCode", "Item_Code")),
            # Z_SalesOrder stores the item description in I_Dscription.
            # Keep ItemName/I_ItemName aliases for compatibility, but prefer
            # the actual I_Dscription value so the Stage S1 form does not
            # display a blank item name when ItemName is not a projected field.
            "item_name": _clean(_value(
                sales_row,
                "I_Dscription",
                "I_Description",
                "I_ItemDescription",
                "ItemName",
                "I_ItemName",
                "Item_Name",
                "ItemDescription",
                "Description",
            )), 
            "quantity": _clean(_value(sales_row, "Quantity", "I_Quantity", "Qty")),
            "weight": weight_raw if _text(weight_raw) else "",
            "rows": rows,
        })

    return groups, (total_weight if have_weight else None), target_batch_status


async def build_stage1_custom_context(stage_context: dict[str, Any]) -> dict[str, Any]:
    """Build the context consumed by stage_custom_s1.html."""
    fms = stage_context.get("fms") or {}
    table_name = _text(fms.get("table_name"))
    fms_name = _text(fms.get("fms_name"))
    row = stage_context.get("row") or {}
    publish_names = await _load_publish_names(table_name)

    enabled = table_name.casefold() == SALES_ORDER_TABLE.casefold() or _norm(fms_name) == _norm("Pack to Dispatch Details")
    if not enabled:
        return {"enabled": False}

    stage = stage_context.get("stage") or {}
    stage_no = _text(stage.get("STAGE_NO")) or "S1"
    stage_locked = not bool(stage_context.get("editable"))
    task_status = _value(row, "S1_TaskStatus")
    if _is_complete(task_status):
        stage_locked = True

    order_no = _text(_value(row, "I_DocNum", "DocNum", "SalesOrderNo", "SalesOrderNumber"))
    create_ts = _text(_value(row, "I_CreateTS", "CreateTS", "CreateTimestamp", "CreatedTS"))

    # The main stage row may be only one physical sales-order line.  The custom
    # form is line-aware, so load all matching lines from the same SO/create-ts.
    sales_rows: list[dict[str, Any]] = []
    if table_name.casefold() == SALES_ORDER_TABLE.casefold() and order_no:
        filters = [
            {"I_DocNum": order_no, "I_CreateTS": create_ts} if create_ts else {"I_DocNum": order_no},
            {"DocNum": order_no, "CreateTS": create_ts} if create_ts else {"DocNum": order_no},
            {"I_DocNum": order_no},
            {"DocNum": order_no},
        ]
        for flt in filters:
            try:
                candidate = await table_api.get_table(SALES_ORDER_TABLE, filters=flt)
            except Exception:
                candidate = []
            if candidate:
                sales_rows = candidate
                break

    if not sales_rows:
        sales_rows = [row] if isinstance(row, dict) else []

    # Preserve unique physical rows.
    unique_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for sales_row in sales_rows:
        rid = _row_id(sales_row)
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
        unique_rows.append(sales_row)
    sales_rows = unique_rows

    current_pick_id = _pick_id(row)
    if not current_pick_id:
        for sr in sales_rows:
            current_pick_id = _pick_id(sr)
            if current_pick_id:
                break

    picker_value = _text(_value(row, "S1_Picker"))
    if not picker_value and current_pick_id:
        for sr in sales_rows:
            if _norm(_pick_id(sr)) == _norm(current_pick_id):
                picker_value = _text(_value(sr, "S1_Picker"))
                if picker_value:
                    break
    picker_values = _parse_list(picker_value)

    picker_options = await _load_picker_options(_value(row, "I_Location", "Location"))
    option_keys = {_norm(x["name"]) for x in picker_options}
    for name in picker_values:
        if _norm(name) not in option_keys:
            picker_options.append({"name": name, "label": name})

    batch_groups, batch_weight_total, batch_status = await _build_batch_groups(
        sales_rows, order_no, create_ts
    )

    top_fields = {
        "plhelper": _field_from_row(row, "S1_PLHelper", _publish_label(publish_names, "S1_PLHelper", "S1 Planned Helper")),
        "plbasets": _field_from_row(row, "S1_PLBaseTS", _publish_label(publish_names, "S1_PLBaseTS", "S1 Base TS")),
        "leadtimemins": _field_from_row(row, "S1_LeadTimeMins", _publish_label(publish_names, "S1_LeadTimeMins", "S1 Lead Time Mins")),
        "pltimestamp": _field_from_row(row, "S1_PLTimeStamp", _publish_label(publish_names, "S1_PLTimeStamp", "S1 Planned Timestamp")),
        "doer": _field_from_row(row, "S1_Doer", _publish_label(publish_names, "S1_Doer", "S1 Doer"), "S1_DoerEmpCode"),
        "alhelper": _field_from_row(row, "S1_ALHelper", _publish_label(publish_names, "S1_ALHelper", "S1 Actual Helper")),
        "ltgrantmins": _field_from_row(row, "S1_LTGrantMins", _publish_label(publish_names, "S1_LTGrantMins", "S1_LeadTimeGrantMins"), "S1_LeadTimeGrantMins", "S1_LTGrant"),
        "delay": _field_from_row(row, "S1_DelayMins", _publish_label(publish_names, "S1_DelayMins", "S1_DelayTime"), "S1_DelayTime"),
        "altimestamp": _field_from_row(row, "S1_ALTimeStamp", _publish_label(publish_names, "S1_ALTimeStamp", "S1_ActualTimestamp"), "S1_ActualTimestamp", "S1_ActualDateTime", "S1_ALTime"),
        "taskstatus": _field_from_row(row, "S1_TaskStatus", _publish_label(publish_names, "S1_TaskStatus", "S1 Task Status")),
    }

    doc_fields = {
        "no_packages": _field_from_row(row, "S1_NoOfPackages", _publish_label(publish_names, "S1_NoOfPackages", "No of Packages"), "NoOfPackages"),
        "weight_kgs": _field_from_row(row, "S1_WeightKgs", _publish_label(publish_names, "S1_WeightKgs", "Weight Kgs"), "WeightKg"),
        "remarks_doc": _field_from_row(row, "S1_Remarks", _publish_label(publish_names, "S1_Remarks", "Remarks"), "S1_RemarksDoc"),
        "pack_image_1": _field_from_row(row, "S1_PackingImg1", _publish_label(publish_names, "S1_PackImage1", "Packing Img1"), "S1_PackImage1", "S1_PackingImage1"),
        "pack_image_2": _field_from_row(row, "S1_PackingImg2", _publish_label(publish_names, "S1_PackImage2", "Packing Img2"), "S1_PackImage2", "S1_PackingImage2"),
    }

    return {
        "enabled": True,
        "stage_locked": stage_locked,
        "embedded": bool(stage_context.get("embedded")),
        "appended_saved": bool(stage_context.get("appended_saved")),
        "top_fields": top_fields,
        "order_info": {
            "sales_order_no": order_no,
            "sales_order_no_caption": _publish_label(publish_names, "I_DocNum", "Sales Order No"),
            "order_processing_ts": _clean(_value(row, "I_OrderProcessingTS", "OrderProcessingTS", "Order_Processing_TS")),
            "order_processing_ts_caption": _publish_label(publish_names, "I_OrderProcessingTS", "Order Processing TS"),
            "location": _clean(_value(row, "I_Location", "Location")),
            "location_caption": _publish_label(publish_names, "I_Location", "Location"),
            "whse_code": _clean(_value(row, "I_WhseCode", "WhseCode", "WarehouseCode", "Whse_Code")),
            "whse_code_caption": _publish_label(publish_names, "I_WhseCode", "Whse Code"),
            "shipping_type": _clean(_value(row, "I_SOShippingTypeU", "SOShippingTypeU", "ShippingType")),
            "shipping_type_caption": _publish_label(publish_names, "I_SOShippingTypeU", "Shipping Type"),
            "pick_pack_remarks": _clean(_value(row, "I_PickNPackRemarks", "PickNPackRemarks", "Pick_Pack_Remarks")),
            "pick_pack_remarks_caption": _publish_label(publish_names, "I_PickNPackRemarks", "Pick N Pack Remarks"),
        },
        "pick_id": current_pick_id,
        "picker": {
            "field": "S1_Picker",
            "caption": _publish_label(publish_names, "S1_Picker", "S1 Picker"),
            "value": ", ".join(picker_values),
            "values": picker_values,
            "options": sorted(picker_options, key=lambda x: x["name"].casefold()),
        },
        "batch_groups": batch_groups,
        "batch_status": batch_status.casefold(),
        "batch_empty_message": (
            f"No {batch_status.casefold()} batch rows found for this line."
        ),
        "batch_weight_total": batch_weight_total,
        "checkbox_field": "S1_Submission",
        "remarks_row_field": "S1_Remarks",
        "doc_level_fields": doc_fields,
        "publish_names": publish_names,
    }


async def get_busy_picker_assignments(current_pick_id: str = "") -> list[dict[str, Any]]:
    """Return active S1 picker assignments grouped by PickID.

    A picker is BUSY only while the picker is still present in ``S1_Picker``
    and the related PickID is not fully completed. A picker becomes AVAILABLE
    in either of these cases:
      1. the picker is removed from the S1_Picker assignment; or
      2. every related Sales Order row for that PickID has an S1 Actual Timestamp.

    The complete source is refreshed for every board request so removal and
    completion are reflected immediately.
    """
    rows = await get_sales_order_picker_rows(force_refresh=True)
    current_key = _norm(current_pick_id)

    # A PickID can span multiple physical Z_SalesOrder rows. Evaluate completion
    # at PickID level so one completed line cannot accidentally release a picker
    # while another line of the same PickID is still active.
    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pick_id = _pick_id(row) or _row_id(row)
        key = _norm(pick_id) or f"row:{_row_id(row)}"
        grouped_rows.setdefault(key, []).append(row)

    result: list[dict[str, Any]] = []
    for key, group_rows in grouped_rows.items():
        pick_id = _pick_id(group_rows[0]) or _row_id(group_rows[0]) or "-"
        if current_key and key == current_key:
            continue

        all_rows_completed = all(
            bool(_text(_value(
                row,
                "S1_ALTimeStamp",
                "S1_ALTime",
                "S1_ActualTimestamp",
                "S1_ActualDateTime",
            )))
            for row in group_rows
        )
        if all_rows_completed:
            continue

        pickers: list[str] = []
        picker_keys: set[str] = set()
        row_ids: list[str] = []

        for row in group_rows:
            actual = _value(
                row,
                "S1_ALTimeStamp",
                "S1_ALTime",
                "S1_ActualTimestamp",
                "S1_ActualDateTime",
            )
            if _text(actual):
                continue

            for picker in _parse_list(_value(row, "S1_Picker")):
                picker_key = _norm(picker)
                if picker_key and picker_key not in picker_keys:
                    picker_keys.add(picker_key)
                    pickers.append(picker)

            rid = _row_id(row)
            if rid and rid not in row_ids:
                row_ids.append(rid)

        # This PickID has no currently-active picker assignment.
        if not pickers:
            continue

        result.append({
            "pick_id": pick_id,
            "pickers": pickers,
            "row_ids": row_ids,
        })

    result.sort(key=lambda item: str(item.get("pick_id") or "").casefold())
    return result

