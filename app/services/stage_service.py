
"""
Stage Form orchestration - resolves whether a given stage of a task is
currently editable, builds its fields, and persists Save/Submit actions.

Application details, timeline and stage forms are deliberately assembled
by *separate* services - the Stage Form never reaches into how the
Application Form or dashboard are built.
"""
from __future__ import annotations

from typing import Any

from app.api import table_api
from app.api import task_api
from app.api.metadata_api import get_metadata
from app.services import form_service, task_service, employee_availability_service, metadata_service
from app.utils.permission_helper import ACTION_EDIT_CURRENT_STAGE, can
from app.utils.status_calculator import _resolve_stage_doer_info


class StageNotFoundError(Exception):
    """Raised when a Stage No does not exist in FMS_Config for this task's FMS."""


async def get_stage_context(task_id: str, stage_no: str, table_name: str | None = None, app_id: str | None = None, primary_key: str | None = None) -> dict[str, Any]:
    context = await task_service.get_task_context(task_id, table_name=table_name, app_id=app_id, primary_key=primary_key)

    def _stage_no_key(value: Any) -> str:
        text = str(value or "").strip().upper()
        if text.startswith("STAGE"):
            text = text[5:].strip()
        if text.startswith("S") and text[1:].isdigit():
            return text[1:]
        return text

    requested_stage_key = _stage_no_key(stage_no)
    stage = next(
        (
            s for s in context["stage_configs"]
            if _stage_no_key(s.get("STAGE_NO")) == requested_stage_key
        ),
        None,
    )
    if stage is None:
        raise StageNotFoundError(f"Stage '{stage_no}' is not defined for this application.")

    current_stage = context["current_stage"]
    is_current = bool(
        current_stage
        and _stage_no_key(current_stage.get("STAGE_NO")) == requested_stage_key
    )

    resolved_app_id = (context.get("fms") or {}).get("app_id") or app_id
    resolved_fms_name = (context.get("fms") or {}).get("fms_name")
    login_type = await metadata_service.get_app_login_type(app_id=resolved_app_id, fms_name=resolved_fms_name)
    app_type = await metadata_service.get_app_type(app_id=resolved_app_id, fms_name=resolved_fms_name)
    if "fms" in context and isinstance(context["fms"], dict):
        context["fms"]["login_type"] = login_type
        context["fms"]["type"] = app_type

    kpi_doer_code = None
    has_kpi_row = False
    if app_type == "F":
        kpi_doer_code, has_kpi_row = await metadata_service.get_task_kpi_doer(resolved_app_id, task_id, stage_no)

    stage_doer, stage_doer_code = _resolve_stage_doer_info(stage, context.get("row") or {})

    perm_context = {
        "task_id": task_id,
        "stage_no": stage_no,
        "stage": stage,
        "current_stage": current_stage,
        "stage_configs": context.get("stage_configs") or [],
        "fms": context.get("fms") or {},
        "app_id": resolved_app_id,
        "login_type": login_type,
        "app_type": app_type,
        "kpi_doer_code": kpi_doer_code,
        "has_kpi_row": has_kpi_row,
        "stage_doer": stage_doer,
        "stage_doer_code": stage_doer_code,
        "doer_code": stage_doer_code or stage_doer,
    }
    try:
        from app.services.hierarchy_service import get_hierarchy_map
        await get_hierarchy_map()
    except Exception as hier_err:
        print(f"[Stage Permission] Hierarchy load warning: {hier_err}")

    # For non-admin roles:
    # If app_type == 'F': Can_View and Can_Edit in FMS_Application_Rights are IGNORED.
    # View/Edit come only from Rules A-D.
    # If app_type != 'F': check FMS_Application_Rights for Can View.
    if app_type != "F":
        try:
            from app.auth.request_context import get_current_identity
            ident = get_current_identity()
            if ident and str(getattr(ident, "role_id", "") or "").strip() not in ("1", "2", "3"):
                # All NOR applications have Can View for all users, all Role IDs
                if login_type == "NOR":
                    perm_context["can_view_rights"] = True
                else:
                    u_ec = str(getattr(ident, "employee_code", "") or "").strip()
                    u_id = str(getattr(ident, "user_id", "") or "").strip()
                    if u_ec or u_id:
                        rights = await metadata_service.get_user_application_rights(u_ec, user_id=u_id)
                        perm_context["can_view_rights"] = metadata_service.check_user_can_view_app(rights, resolved_app_id, stage_no=stage_no)
        except Exception as r_err:
            print(f"[Stage Permission] Rights check warning: {r_err}")

    editable = is_current and can(ACTION_EDIT_CURRENT_STAGE, perm_context)
    context["editable"] = editable
    context["can_edit"] = editable
    context["can_view"] = can("can_view", perm_context)

    # Fixed: Rely on get_metadata to ensure TM_Form_Settings are merged correctly.
    # Stage 3 custom forms can still be rendered from the task row when the
    # upstream metadata endpoint is temporarily unavailable. Do not change
    # metadata behavior for any other stage.
    try:
        table_columns = await get_metadata(context["fms"]["table_name"])
    except Exception as metadata_exc:
        # Recreated SQL tables can temporarily expose incomplete/unsupported
        # metadata even though the actual row is readable.  Custom Pack-to-
        # Dispatch stages are row-driven and do not need metadata to render.
        # Fall back to the columns present on the row instead of turning a
        # successful task lookup into the generic 500 page.
        stage_upper = str(stage_no).strip().upper()
        if stage_upper in {"S1", "1", "S2", "2", "S3", "3", "S4", "4", "S5", "5", "S6", "6"}:
            print(
                f"[{stage_upper} FORM LOAD] Metadata API failed; continuing with "
                "row-derived fields. "
                f"table='{context['fms'].get('table_name')}' error={metadata_exc}"
            )
            row_for_fallback = context.get("row") or {}
            stage_num = stage_upper[1:] if stage_upper.startswith("S") and stage_upper[1:].isdigit() else stage_upper
            prefix = "S" + stage_num + "_"
            table_columns = [
                {"name": str(key), "input_type": "text", "nullable": True}
                for key in row_for_fallback.keys()
                if str(key).upper().startswith(prefix)
            ]
            for required_name in (
                (next((str(x.get("PLANNED_COLUMN")) for x in context.get("stage_configs", []) if str(x.get("STAGE_NO") or "").strip().upper() == stage_upper and x.get("PLANNED_COLUMN")), None)),
                (next((str(x.get("ACTUAL_COLUMN")) for x in context.get("stage_configs", []) if str(x.get("STAGE_NO") or "").strip().upper() == stage_upper and x.get("ACTUAL_COLUMN")), None)),
            ):
                if required_name and not any(
                    str(col.get("name", "")).casefold() == required_name.casefold()
                    for col in table_columns
                ):
                    table_columns.append({"name": required_name, "input_type": "text", "nullable": True})
        else:
            raise
    
    fields = form_service.build_stage_form(
        stage=stage, 
        row=context["row"], 
        editable=editable, 
        table_columns=table_columns
    )
    
    timeline_entry = next(
        (t for t in context["timeline"] if str(t["stage_no"]) == str(stage_no)),
        None,
    )

    return {
        **context,
        "stage": stage,
        "is_current": is_current,
        "editable": editable,
        "fields": fields,
        "timeline_entry": timeline_entry,
    }


def _context_row_id(context: dict[str, Any], fallback_task_id: str) -> Any:
    """Resolve the real table primary-key value for the current task row.

    Dashboard/task ids and the physical table RowID can diverge.  Always prefer
    the primary-key value from the loaded row so stage values are written to the
    same record the form is displaying.
    """
    primary_key = str(context.get("primary_key") or "").strip()
    row = context.get("row") or {}

    if primary_key and isinstance(row, dict):
        if primary_key in row and row.get(primary_key) not in (None, ""):
            return row.get(primary_key)

        target = primary_key.lower()
        compact = "".join(ch for ch in target if ch.isalnum())
        for key, value in row.items():
            key_text = str(key).lower()
            key_compact = "".join(ch for ch in key_text if ch.isalnum())
            if (key_text == target or key_compact == compact) and value not in (None, ""):
                return value

    return fallback_task_id



def _row_value(row: dict[str, Any], *names: str) -> Any:
    if not isinstance(row, dict):
        return None
    targets = {"".join(ch for ch in str(name).casefold() if ch.isalnum()) for name in names}
    for key, value in row.items():
        if "".join(ch for ch in str(key).casefold() if ch.isalnum()) in targets:
            return value
    return None

async def save_stage(
    task_id: str,
    stage_no: str,
    payload: dict[str, Any],
    table_name: str | None = None,
    app_id: str | None = None,
    primary_key: str | None = None,
    stage_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    context = stage_context or await get_stage_context(
        task_id, stage_no, table_name=table_name, app_id=app_id, primary_key=primary_key
    )
    if not context.get("editable"):
        raise PermissionError("This stage is not currently editable.")

    current_row = context.get("row") or {}
    work_date = employee_availability_service.resolve_work_date(None, payload, current_row)
    await employee_availability_service.validate_new_assignment_changes(
        payload, current_row, work_date=work_date
    )
    # S1_Picker has additional Pick/Pack-specific Busy checks; the forms route
    # validates it before side effects, while this check protects other callers.
    if any("".join(ch for ch in str(key).casefold() if ch.isalnum()) == "s1picker" for key in (payload or {})):
        from app.services import stage1_custom_service
        selected_pickers = stage1_custom_service._parse_list(payload.get("S1_Picker"))
        custom_context = await stage1_custom_service.build_stage1_custom_context(context)
        existing_pickers = list((custom_context.get("picker") or {}).get("values") or [])
        await stage1_custom_service.validate_picker_changes(
            selected_pickers,
            existing_pickers,
            location=_row_value(context.get("row") or {}, "I_Location", "Location"),
            work_date=employee_availability_service.get_today_ist(),
            current_pick_id=_row_value(context.get("row") or {}, "S1_PickID", "PickID"),
        )

    # Guard against application code writing lifecycle state columns directly:
    # C_RowCycleStatus / C_RowCycleStatusEvent (and legacy I_RowCycleStatus / I_RowCycleStatusEvent)
    # are framework-managed columns and must only be written by row cycle stored procedures.
    cleaned_payload = dict(payload or {})
    for k in list(cleaned_payload.keys()):
        norm_k = "".join(ch for ch in str(k).lower() if ch.isalnum())
        if norm_k in {"crowcyclestatus", "irowcyclestatus", "crowcyclestatusevent", "irowcyclestatusevent"}:
            print(
                f"[ROW CYCLE GUARD] Blocked attempt to write protected row cycle column '{k}' via stage save. "
                "Row cycle status must only be written by framework procedures."
            )
            cleaned_payload.pop(k, None)

    row_id = _context_row_id(context, task_id)
    target_table = (context.get("fms") or {}).get("table_name") or table_name
    print(
        f"[Stage Save] stage={stage_no} task_id={task_id} "
        f"primary_key={context.get('primary_key')} row_id={row_id} "
        f"table={target_table} fields={sorted(cleaned_payload.keys())}"
    )
    return await table_api.update_row(target_table, row_id, cleaned_payload)


def _flatten_procedure_result(result: dict[str, Any] | None) -> dict[str, Any]:
    """Extract a row-like dict from common stored-procedure API response shapes."""
    if not isinstance(result, dict):
        return {}
    candidates = [result]
    for key in ("data", "row", "result", "output", "record"):
        value = result.get(key)
        if isinstance(value, dict):
            candidates.insert(0, value)
    for key in ("rows", "records", "items"):
        value = result.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            candidates.insert(0, value[0])
    merged: dict[str, Any] = {}
    for item in candidates:
        for k, v in item.items():
            merged.setdefault(str(k), v)
    return merged


async def persist_s3_procedure_output(
    task_id: str,
    result: dict[str, Any] | None,
    table_name: str | None = None,
    app_id: str | None = None,
) -> dict[str, Any] | None:
    """Persist SP_FMS_Pack2Dispatch_S3 output into exact S3_* columns.

    The S3 procedure is called with stage S3. If the procedure/API returns
    corresponding values using S3_* keys, they are persisted directly.
    If the procedure returns the same logical fields with an S2_* prefix,
    they are remapped to the matching S3_* column because S3 is the target
    stage. Existing S3 columns are not overwritten with None/blank values.
    """
    raw = _flatten_procedure_result(result)
    if not raw:
        return None

    def norm(key: str) -> str:
        return "".join(ch for ch in str(key).lower() if ch.isalnum())

    normalized = {norm(k): v for k, v in raw.items()}
    mapping = {
        "PLHelper": "S3_PLHelper",
        "PLBaseTS": "S3_PLBaseTS",
        "LeadTimeMins": "S3_LeadTimeMins",
        "PLTimeStamp": "S3_PLTimeStamp",
        "Doer": "S3_Doer",
        "DelDocNo": "S3_DelDocNo",
        "DelLineNo": "S3_DelLineNo",
        "ALHelper": "S3_ALHelper",
        "ALTimeStamp": "S3_ALTimeStamp",
        "KPIApplicable": "S3_KPIApplicable",
        "DelayMins": "S3_DelayMins",
        "TaskStatus": "S3_TaskStatus",
    }

    payload: dict[str, Any] = {}
    for suffix, target in mapping.items():
        candidates = (
            norm(target),
            norm("S2_" + suffix),
            norm(suffix),
        )
        value = None
        for candidate in candidates:
            if candidate in normalized and normalized[candidate] not in (None, ""):
                value = normalized[candidate]
                break
        if value is not None:
            payload[target] = value

    if not payload:
        return None

    context = await get_stage_context(task_id, "S3", table_name=table_name, app_id=app_id)
    row_id = _context_row_id(context, task_id)
    target_table = (context.get("fms") or {}).get("table_name") or table_name
    print(f"[S3 Output Mapping] RowID={row_id} table={target_table} fields={sorted(payload.keys())}")
    result_update = await table_api.update_row(target_table, row_id, payload)
    print(f"[S3 Output Mapping] Result={result_update}")
    return result_update


async def run_linked_stage_procedure(
    task_id: str,
    stage_no: str,
    payload: dict[str, Any] | None = None,
    table_name: str | None = None,
    app_id: str | None = None,
) -> dict[str, Any] | None:
    """Run the FMS-linked post-save workflow for the current stage, if configured."""
    context = await get_stage_context(task_id, stage_no, table_name=table_name, app_id=app_id)
    stage = context.get("stage") or {}
    proc_name = str(
        stage.get("Linked_Stored_Procedure")
        or stage.get("LINKED_STORED_PROCEDURE")
        or stage.get("linked_stored_procedure")
        or ""
    ).strip()
    if not proc_name or proc_name.lower() in ("none", "null"):
        stg_norm = str(stage_no or "").strip().upper()
        if stg_norm == "S2":
            proc_name = "SP_FMS_Pack2Dispatch_S2"
        elif stg_norm == "S3":
            proc_name = "SP_FMS_Pack2Dispatch_S3"
        elif stg_norm == "S4":
            proc_name = "SP_FMS_Pack2Dispatch_S4"
        else:
            return None

    base_payload = dict(payload or {})

    def _normalize_key(value: Any) -> str:
        return "".join(ch for ch in str(value).lower() if ch.isalnum())

    def _pick_value_by_suffix(row: dict[str, Any] | None, suffixes: tuple[str, ...]) -> Any:
        if not isinstance(row, dict):
            return None
        for key, value in row.items():
            key_norm = _normalize_key(key)
            if any(key_norm.endswith(_normalize_key(suffix)) for suffix in suffixes):
                return value
        return None

    # Use the physical table primary key from the loaded row whenever
    # available; the URL/task id is not always identical to RowID.
    row_id_value = _context_row_id(context, task_id)
    try:
        row_id_value = int(str(row_id_value).strip())
    except (TypeError, ValueError):
        pass

    sales_order_no = _pick_value_by_suffix(context.get("row") or {}, ("idocnum", "docnum", "salesorderno"))
    if sales_order_no is None:
        sales_order_no = _pick_value_by_suffix(base_payload, ("idocnum", "docnum", "salesorderno"))
    if sales_order_no is None:
        sales_order_no = row_id_value

    sales_order_entry = _pick_value_by_suffix(context.get("row") or {}, ("idocentry", "docentry", "salesorderentry"))
    if sales_order_entry is None:
        sales_order_entry = _pick_value_by_suffix(base_payload, ("idocentry", "docentry", "salesorderentry"))
    if sales_order_entry is None:
        sales_order_entry = _pick_value_by_suffix(context.get("row") or {}, ("idocnum", "docnum", "salesorderno"))
    if sales_order_entry is None:
        sales_order_entry = _pick_value_by_suffix(base_payload, ("idocnum", "docnum", "salesorderno"))
    if sales_order_entry is None:
        sales_order_entry = row_id_value

    # Linked SPs like SP_FMS_Pack2Dispatch_S2 expect the sales-order entry value
    # (I_DocEntry / DocEntry) to be passed in, not the document number.
    base_payload["RowID"] = row_id_value
    base_payload.setdefault("I_DocEntry", sales_order_entry)
    base_payload.setdefault("DocEntry", sales_order_entry)
    base_payload.setdefault("DocEntrySO", sales_order_entry)
    base_payload.setdefault("SalesOrderNo", sales_order_no)

    target_table = (context.get("fms") or {}).get("table_name") or table_name
    request_payload = {
        "table": target_table,
        "task_id": task_id,
        "stage_no": stage_no,
        "procedure": proc_name,
        "linked_stored_procedure": proc_name,
        "data": base_payload,
    }

    return await task_api.save_task(request_payload)


async def run_named_stage_procedure(
    task_id: str,
    stage_no: str,
    procedure_name: str,
    payload: dict[str, Any] | None = None,
    table_name: str | None = None,
    app_id: str | None = None,
) -> dict[str, Any] | None:
    """Run a specific stored procedure in stage context regardless of linked-proc config."""
    proc_name = str(procedure_name or "").strip()
    if not proc_name:
        return None

    context = await get_stage_context(task_id, stage_no, table_name=table_name, app_id=app_id)
    physical_row_id = _context_row_id(context, task_id)
    target_table = (context.get("fms") or {}).get("table_name") or table_name
    request_payload = {
        "table": target_table,
        "task_id": task_id,
        "stage_no": stage_no,
        "procedure": proc_name,
        "linked_stored_procedure": proc_name,
    }
    proc_data = dict(payload or {})
    # RowID is authoritative from the physical row loaded for this task unless explicitly provided.
    if "RowID" not in proc_data or proc_data["RowID"] in (None, ""):
        proc_data["RowID"] = physical_row_id
    request_payload["data"] = proc_data

    print(
        "[SP EXECUTION VERIFY] ABOUT TO CALL TASK API: "
        f"procedure='{proc_name}' stage='{stage_no}' task_id='{task_id}' "
        f"physical_row_id='{physical_row_id}' payload={request_payload.get('data')!r}"
    )
    try:
        result = await task_api.save_task(request_payload)
    except Exception as exc:
        print(
            "[SP EXECUTION VERIFY] TASK API FAILED: "
            f"procedure='{proc_name}' stage='{stage_no}' task_id='{task_id}' "
            f"physical_row_id='{physical_row_id}' error={exc!r}"
        )
        raise
    print(
        "[SP EXECUTION VERIFY] TASK API RETURNED: "
        f"procedure='{proc_name}' stage='{stage_no}' task_id='{task_id}' "
        f"physical_row_id='{physical_row_id}' result={result!r}"
    )
    if isinstance(result, dict) and (
        str(result.get("status") or "").strip().casefold() in {"error", "failed", "failure"}
        or result.get("success") is False
    ):
        print(
            "[SP EXECUTION VERIFY] PROCEDURE RESPONSE INDICATES FAILURE: "
            f"procedure='{proc_name}' result={result!r}"
        )
        detail = result.get("error") or result.get("message") or result.get("detail") or result
        raise RuntimeError(
            f"Stored procedure '{proc_name}' failed for RowID={physical_row_id}: {detail}"
        )
    return result


