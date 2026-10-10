"""
Developer Hub (Integration Hub) - the feature that turns this portal into
a true workflow engine: every task/stage is addressable, every route is
generated (never hardcoded), and the shared APIs can be explored/tested
directly from the browser.
"""
from __future__ import annotations

import io
import os
import re
from typing import Any

import qrcode
from fastapi import APIRouter, Body, Query, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse

from app.api.client import ApiError, api_client
from app.api.endpoints import API_ENDPOINTS
from app.api import table_api
from app.api.metadata_api import save_apply_filter_flags, save_column_settings
from app.services import form_service, metadata_service, stage_service, task_service
from app.templates_config import templates
from app.utils import route_builder
from app.utils.stage_calculator import sorted_stages
from app.utils.input_mapper import map_input_type

router = APIRouter(prefix="/developer", tags=["developer"])

STATIC_ROUTES = {
    "Dashboard": route_builder.dashboard_route(),
    "My Tasks": route_builder.my_tasks_route(),
    "All Tasks": route_builder.all_tasks_route(),
    "Developer Hub": route_builder.developer_hub_route(),
    "API Explorer": route_builder.developer_apis_route(),
    "Route Generator": route_builder.developer_routes_route(),
    "Deep Links": route_builder.developer_deeplinks_route(),
    "Metadata Explorer": route_builder.developer_metadata_route(),
    "Form Preview": route_builder.developer_form_preview_route(),
    "Custom Forms": route_builder.developer_custom_forms_route(),
}


@router.get("", response_class=HTMLResponse)
async def developer_hub(request: Request):
    return templates.TemplateResponse(request, "developer/index.html", {})


def _custom_forms_absolute_url(request: Request, relative_route: str) -> str:
    """Build Custom Form URLs from the current request host/port.

    This keeps local links on 8000/8001 (or any active port) and also works
    behind a deployed host, instead of relying on settings.public_base_url.
    """
    return f"{str(request.base_url).rstrip('/')}{relative_route}"


@router.get("/custom-forms", response_class=HTMLResponse)
async def custom_forms(
    request: Request,
    app_id: str | None = Query(None),
):
    """Developer page for one canonical custom-form link per application stage.

    FMS_Config can contain multiple rows for the same APP_ID + STAGE_NO because
    the stage may have different CONDITIONS / DOER assignments. The developer
    UI shows the form only once per stage and stores one dynamic Stage_Form_Link
    on every matching FMS_Config row. The task id remains a runtime placeholder.
    """
    fms_options = await metadata_service.get_all_fms_options()
    selected = None
    custom_forms_list: list[dict[str, Any]] = []
    error = None

    if app_id:
        selected = next(
            (f for f in fms_options if str(f.get("app_id") or "").strip() == str(app_id).strip()),
            None,
        )

    def safe_filename_part(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_\-]", "", str(value or "").strip())

    def custom_stage_exists(table_name: str, stage_no: str) -> tuple[bool, str]:
        table = safe_filename_part(table_name)
        stage = safe_filename_part(stage_no.upper())
        candidates = [
            f"app/templates/custom_forms/{table}_stage_{stage}.html",
            f"app/templates/custom_forms/{table}_{stage}.html",
            f"app/templates/custom_forms/stage_{stage}.html",
        ]
        for path in candidates:
            if os.path.exists(path):
                return True, path.replace("app/templates/", "")
        if stage == "S1" and table.casefold() == "z_salesorder":
            return True, "forms/stage_custom_s1.html (special renderer)"
        return False, ""


    if selected:
        table_name = str(selected.get("table_name") or "").strip()
        fms_name = str(selected.get("fms_name") or selected.get("app_id") or "Application").strip()
        app_key = str(selected.get("app_id") or app_id or "").strip()

        try:
            raw_stage_rows = await metadata_service.get_fms_config(app_id=app_key)
        except Exception as exc:
            print(f"[CUSTOM FORMS] Unable to load stages for app {app_key}: {exc!r}")
            raw_stage_rows = []

        # Keep one representative config row per STAGE_NO while remembering all
        # duplicate condition rows so the UI never renders duplicate forms.
        unique_stage_rows: dict[str, dict[str, Any]] = {}
        duplicate_counts: dict[str, int] = {}
        for stage_row in raw_stage_rows:
            stage_no = str(stage_row.get("STAGE_NO") or "").strip().upper()
            if not stage_no:
                continue
            if stage_no not in unique_stage_rows:
                unique_stage_rows[stage_no] = stage_row
                duplicate_counts[stage_no] = 1
            else:
                duplicate_counts[stage_no] += 1

        for stage_no in sorted(unique_stage_rows.keys(), key=lambda x: (route_builder.stage_sort_key(x), x)):
            stage_row = unique_stage_rows[stage_no]
            exists, template = custom_stage_exists(table_name, stage_no)
            if not exists:
                continue

            stage_name = str(stage_row.get("STAGE_NAME") or "").strip() or "Custom Form"
            # Display/Test URL uses Task ID 1.  This is only a preview value.
            sample_route = route_builder.stage_form_route("1", stage_no, app_id=app_key)
            sample_url = _custom_forms_absolute_url(request, sample_route)
            # Saved configuration is dynamic: do not persist task id 1.
            dynamic_route = route_builder.stage_form_route("{TASK_ID}", stage_no, app_id=app_key)
            dynamic_url = _custom_forms_absolute_url(request, dynamic_route)

            saved_link = str(
                stage_row.get("STAGE_FORM_LINK")
                or stage_row.get("Stage_Form_Link")
                or stage_row.get("stage_form_link")
                or ""
            ).strip()

            custom_forms_list.append({
                "type": "Stage",
                "name": f"Stage {stage_no} — {stage_name}",
                "stage_no": stage_no,
                "stage_name": stage_name,
                "template": template,
                "sample_url": sample_url,
                "sample_route": sample_route,
                "dynamic_url": dynamic_url,
                "saved_link": saved_link,
                "duplicate_count": duplicate_counts.get(stage_no, 1),
                "app_id": app_key,
            })

    return templates.TemplateResponse(request, "developer/custom_forms.html", {
        "fms_options": fms_options,
        "selected": selected,
        "app_id": app_id or "",
        "custom_forms_list": custom_forms_list,
        "error": error,
        "sample_task_id": "1",
    })


@router.post("/custom-forms/save-stage")
async def save_custom_stage_config(request: Request, payload: dict[str, Any] = Body(...)):
    """Save only the dynamic Stage_Form_Link to FMS_Config.

    The Stage_Form_Link deliberately contains {TASK_ID}; Task 1 is only the
    preview/open example in Developer Hub and is never persisted.
    """
    app_id = str(payload.get("app_id") or "").strip()
    stage_no = str(payload.get("stage_no") or "").strip().upper()

    if not app_id or not stage_no:
        return JSONResponse(
            {"ok": False, "message": "app_id and stage_no are required."},
            status_code=400,
        )

    rows = await metadata_service.get_fms_config(app_id=app_id)
    matches = [
        row for row in rows
        if str(row.get("STAGE_NO") or "").strip().upper() == stage_no
    ]
    if not matches:
        return JSONResponse(
            {"ok": False, "message": f"No FMS_Config row found for APP_ID={app_id}, STAGE_NO={stage_no}."},
            status_code=404,
        )

    dynamic_route = route_builder.stage_form_route("{TASK_ID}", stage_no, app_id=app_id)
    dynamic_url = _custom_forms_absolute_url(request, dynamic_route)
    updated = 0
    failures: list[str] = []

    for row in matches:
        config_id = row.get("config_id")
        if config_id in (None, ""):
            for key, value in row.items():
                if str(key).casefold() in {"config_id", "configid", "id"}:
                    config_id = value
                    break
        if config_id in (None, ""):
            failures.append("Missing config_id on one FMS_Config row")
            continue

        try:
            await table_api.update_row(
                "FMS_Config",
                config_id,
                {
                    "Stage_Form_Link": dynamic_url,
                },
            )
            updated += 1
        except Exception as exc:
            failures.append(f"config_id={config_id}: {exc}")

    metadata_service.invalidate_fms_config_cache(app_id=app_id)

    if failures:
        return JSONResponse(
            {
                "ok": updated > 0,
                "message": f"Updated {updated} row(s); {len(failures)} row(s) failed.",
                "updated": updated,
                "failures": failures,
                "stage_form_link": dynamic_url,
            },
            status_code=207 if updated else 502,
        )

    return JSONResponse({
        "ok": True,
        "updated": updated,
        "stage_form_link": dynamic_url,
        "message": f"Stage form URL saved to FMS_Config for APP_ID {app_id}.",
    })


# ---------------------------------------------------------------- API Explorer
@router.get("/apis", response_class=HTMLResponse)
async def api_explorer(request: Request):
    endpoints = [
        {"key": key, **spec} for key, spec in API_ENDPOINTS.items()
    ]
    return templates.TemplateResponse(request, "developer/apis.html", {"endpoints": endpoints})


@router.post("/apis/test")
async def api_explorer_test(payload: dict[str, Any] = Body(...)):
    endpoint_key = payload.get("endpoint_key")
    path_params = payload.get("path_params") or {}
    query_params = payload.get("query_params") or {}

    if endpoint_key not in API_ENDPOINTS:
        return JSONResponse({"error": f"Unknown endpoint '{endpoint_key}'"}, status_code=400)

    try:
        result = await api_client.request(endpoint_key, path_params=path_params, query_params=query_params)
        return JSONResponse({"ok": True, "data": result})
    except ApiError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "status_code": exc.status_code}, status_code=502)


# ---------------------------------------------------------------- Route Generator
@router.get("/routes", response_class=HTMLResponse)
async def route_generator(request: Request, task_id: str | None = Query(None)):
    route_map: dict[str, str] = {}
    error = None
    task_context = None

    if task_id:
        try:
            task_context = await task_service.get_task_context(task_id)
            stage_numbers = [s["STAGE_NO"] for s in task_context["stage_configs"]]
            current_stage_no = task_context["current_stage"]["STAGE_NO"] if task_context["current_stage"] else None
            route_map = route_builder.build_task_route_map(task_id, stage_numbers, current_stage_no)
        except task_service.TaskNotFoundError:
            error = f"Task '{task_id}' was not found."

    return templates.TemplateResponse(request, "developer/routes.html", {
        "task_id": task_id or "", "route_map": route_map,
        "task_context": task_context, "error": error,
    })


# ---------------------------------------------------------------- Deep Links
@router.get("/deeplinks", response_class=HTMLResponse)
async def deep_links(request: Request, task_id: str | None = Query(None)):
    task_route_map: dict[str, str] = {}
    error = None

    if task_id:
        try:
            task_context = await task_service.get_task_context(task_id)
            stage_numbers = [s["STAGE_NO"] for s in task_context["stage_configs"]]
            current_stage_no = task_context["current_stage"]["STAGE_NO"] if task_context["current_stage"] else None
            task_route_map = route_builder.build_task_route_map(task_id, stage_numbers, current_stage_no)
        except task_service.TaskNotFoundError:
            error = f"Task '{task_id}' was not found."

    return templates.TemplateResponse(request, "developer/deeplinks.html", {
        "task_id": task_id or "", "static_routes": STATIC_ROUTES,
        "task_route_map": task_route_map, "error": error,
    })


@router.get("/qr")
async def qr_code(data: str = Query(...)):
    absolute = data if data.startswith("http") else route_builder.absolute_url(data)
    img = qrcode.make(absolute)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")


# ---------------------------------------------------------------- Metadata Explorer
@router.get("/metadata", response_class=HTMLResponse)
async def metadata_explorer(
    request: Request,
    app_id: str | None = Query(None),
    task_id: str | None = Query(None),
):
    fms_options = await metadata_service.get_all_fms_options()
    selected = None
    stages: list[dict[str, Any]] = []
    columns: list[dict[str, Any]] = []
    primary_key = None

    current_role_id = 1 
    has_form_settings_access = current_role_id in [1, 2, 3]

    # Extract all distinct tables for the typeable dropdown
    all_tables = sorted(list(set(f["table_name"] for f in fms_options if f.get("table_name"))))

    if app_id:
        selected = next((f for f in fms_options if str(f["app_id"]) == str(app_id)), None)
        if selected:
            stage_rows = await metadata_service.get_fms_config(app_id=app_id)
            stages = sorted_stages(stage_rows)
            columns = await metadata_service.get_table_metadata(selected["table_name"])
            primary_key = await metadata_service.resolve_primary_key(selected["table_name"])

            for col in columns:
                if "input_type" not in col:
                    col["input_type"] = map_input_type(col["name"], col["data_type"])

    return templates.TemplateResponse(request, "developer/metadata.html", {
        "fms_options": fms_options, "selected": selected,
        "app_id": app_id or "", "stages": stages, "columns": columns, "primary_key": primary_key,
        "has_form_settings_access": has_form_settings_access,
        "task_id": task_id or "",
        "all_tables": all_tables,
    })


@router.post("/metadata/save-settings")
async def save_form_settings(request: Request):
    # A metadata row contains many controls (publish name, input type, access,
    # visibility, mandatory, dropdown settings, conditional UI, display order,
    # etc.).  Large SQL tables therefore exceed Starlette's default 1000-field
    # multipart parser limit.  This page is developer/admin-only, so use a
    # deliberately higher bounded limit instead of truncating settings.
    form_data = await request.form(max_fields=20000, max_files=1000)
    table_name = form_data.get("table_name")
    
    if not table_name:
        return RedirectResponse(url=request.headers.get("referer", "/developer/metadata"), status_code=303)

    column_names = set()
    for key in form_data.keys():
        if key.startswith("input_type_"):
            column_names.add(key.replace("input_type_", ""))

    settings_payload = []
    apply_filter_by_column: dict[str, str] = {}
    for col in column_names:
        access_val = form_data.get(f"access_{col}", "editable")
        apply_filter_value = "Y" if form_data.get(f"apply_filter_{col}") == "1" else "N"
        apply_filter_by_column[col] = apply_filter_value
        
        settings_payload.append({
            "column_name": col,
            "input_type": form_data.get(f"input_type_{col}", "text"),
            "publish_name": form_data.get(f"publish_name_{col}", ""),
            "apply_filter": apply_filter_value,
            "Apply_Filter": apply_filter_value,
            "APPLY_FILTER": apply_filter_value,
            "is_hidden": 1 if form_data.get(f"is_hidden_{col}") == "1" else 0,
            "is_mandatory": 1 if form_data.get(f"is_mandatory_{col}") == "1" else 0,
            "is_readonly": 1 if access_val == "readonly" else 0,
            "is_editable": 1 if access_val == "editable" else 0,
            "text_rows": form_data.get(f"text_rows_{col}", 2),
            "select_mode": form_data.get(f"select_mode_{col}", ""),
            "select_list_values": form_data.get(f"select_list_values_{col}", ""),
            "select_table": form_data.get(f"select_table_{col}", ""),
            "select_column": form_data.get(f"select_column_{col}", ""),
            "dd_depends_on": form_data.get(f"dd_depends_on_{col}", ""),
            "dd_filter_column": form_data.get(f"dd_filter_column_{col}", ""),
            "show_if_column": form_data.get(f"show_if_column_{col}", ""),
            "show_if_operator": form_data.get(f"show_if_operator_{col}", ""),
            "show_if_value": form_data.get(f"show_if_value_{col}", ""),
            "display_order": form_data.get(f"display_order_{col}", 0),
        })

    try:
        await save_column_settings(table_name, settings_payload)
        await save_apply_filter_flags(table_name, apply_filter_by_column)
    except Exception as e:
        print(f"Error saving settings: {e}")

    referer = request.headers.get("referer", "/developer/metadata")
    return RedirectResponse(url=referer, status_code=303)


@router.get("/metadata/api/columns")
async def api_get_columns(table: str = Query(...)):
    """Internal route to fetch columns for a table dynamically for the UI dropdowns."""
    try:
        cols = await metadata_service.get_table_metadata(table)
        return JSONResponse({"status": "ok", "columns": [c["name"] for c in cols]})
    except Exception as e:
        return JSONResponse({"status": "error", "columns": []})


# ---------------------------------------------------------------- Form Preview
@router.get("/forms", response_class=HTMLResponse)
async def form_preview(request: Request, task_id: str | None = Query(None)):
    preview_sections: list[dict[str, Any]] = []
    application_fields = []
    error = None
    context = None

    if task_id:
        try:
            context = await task_service.get_task_context(task_id)
            application_fields = await form_service.build_application_form(
                context["fms"]["table_name"], context["row"],
            )
            table_columns = await metadata_service.get_table_metadata(context["fms"]["table_name"])
            current_no = context["current_stage"]["STAGE_NO"] if context["current_stage"] else None

            for stage in context["stage_configs"]:
                is_current = stage["STAGE_NO"] == current_no
                fields = form_service.build_stage_form(stage, context["row"], is_current, table_columns)
                timeline_entry = next(
                    (t for t in context["timeline"] if t["stage_no"] == stage["STAGE_NO"]), None,
                )
                preview_sections.append({
                    "stage": stage,
                    "fields": fields,
                    "is_current": is_current,
                    "status": timeline_entry["status"] if timeline_entry else "Pending",
                })
        except task_service.TaskNotFoundError:
            error = f"Task '{task_id}' was not found."

    return templates.TemplateResponse(request, "developer/form_preview.html", {
        "task_id": task_id or "", "application_fields": application_fields,
        "preview_sections": preview_sections, "error": error, "context": context,
    })