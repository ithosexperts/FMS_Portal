"""
Dashboard routes - /dashboard, /mytasks, /tasks.

Loads only FMS_TYPE='F' applications. All filtering (FMS, Status, Doer,
Search) happens server-side; the grid itself is a separate partial so
HTMX can refresh just the rows without a full page reload.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from urllib.parse import urlencode
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from app.api import table_api
from app.services import dashboard_service, form_service, metadata_service, task_service, stage1_custom_service
from app.templates_config import templates
from app.utils import route_builder
from app.utils.delay_calculator import parse_datetime

router = APIRouter(tags=["dashboard"])

STATUSES = ["Completed", "In Progress", "Delayed", "Pending", "Cancelled"]


PAGE_SIZE = 50


def _section_key(name: str) -> str:
    raw = str(name or "").strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return key or "fms"


def _build_query_string(pairs: list[tuple[str, str]], override: dict[str, str] | None = None) -> str:
    merged: list[tuple[str, str]] = []
    override = override or {}
    skip_keys = set(override.keys())
    for key, value in pairs:
        if key in skip_keys:
            continue
        merged.append((key, value))
    for key, value in override.items():
        merged.append((key, value))
    return urlencode(merged, doseq=True)


def _extract_fms_filters(request: Request, allowed_columns: set[str]) -> dict[str, list[str]]:
    """Read repeated ff_<column>=value query params as multi-select filters."""
    extracted: dict[str, list[str]] = {}
    for key in request.query_params.keys():
        if not key.startswith("ff_"):
            continue
        column_name = key[3:]
        if not column_name or column_name.lower() not in allowed_columns:
            continue
        values = [str(v).strip() for v in request.query_params.getlist(key) if str(v).strip()]
        if values:
            extracted[column_name] = values
    return extracted


def _extract_section_fms_filters(
    request: Request,
    section_prefix: str,
    allowed_columns: set[str],
) -> dict[str, list[str]]:
    """Read repeated <section_prefix>_ff_<column>=value query params as multi-select filters."""
    extracted: dict[str, list[str]] = {}
    prefix = f"{section_prefix}_ff_"
    for key in request.query_params.keys():
        if not key.startswith(prefix):
            continue
        column_name = key[len(prefix):]
        if not column_name or column_name.lower() not in allowed_columns:
            continue
        values = [str(v).strip() for v in request.query_params.getlist(key) if str(v).strip()]
        if values:
            extracted[column_name] = values
    return extracted


def _value_matches_any(raw_value: Any, selected_values: list[str]) -> bool:
    if raw_value is None:
        return False
    normalized_value = str(raw_value).strip().lower()
    if not normalized_value:
        return False
    return any(normalized_value == str(v).strip().lower() for v in selected_values)


def _task_matches_search(task: Any, search_text: str) -> bool:
    needle = str(search_text or "").strip().lower()
    if not needle:
        return True
    task_id = str(getattr(task, "task_id", "") or "").strip().lower()
    row_id = str(getattr(task, "row_id_value", "") or "").strip().lower()
    return needle == task_id or needle == row_id


def _row_value_ci(row: dict[str, Any], column_name: str) -> Any:
    target = str(column_name or "").strip().lower()
    for key, value in row.items():
        if str(key).strip().lower() == target:
            return value
    return None


async def _dashboard_context(
    request: Request,
    fms_name: str | None,
    status: str | None,
    doer: str | None,
    search: str | None,
    page: int = 1,
) -> dict:
    progressive_refresh = bool(str(request.query_params.get("_progressive_ts") or "").strip())
    selected_fms_values = [str(v).strip() for v in request.query_params.getlist("fms_name") if str(v).strip()]
    if not selected_fms_values and fms_name:
        selected_fms_values = [str(fms_name).strip()]

    selected_doer_values = [str(v).strip() for v in request.query_params.getlist("doer") if str(v).strip()]
    if not selected_doer_values and doer:
        selected_doer_values = [str(doer).strip()]

    fms_options = await dashboard_service.get_dashboard_fms_options()
    if not selected_fms_values and fms_options:
        first_name = str(fms_options[0].get("fms_name") or "").strip()
        if first_name:
            selected_fms_values = [first_name]

    selected_fms_map = {v.lower(): v for v in selected_fms_values}
    normalized_fms_values: list[str] = []
    selected_fms_options: list[dict[str, Any]] = []
    for option in fms_options:
        option_name = str(option.get("fms_name") or "").strip()
        if option_name and option_name.lower() in selected_fms_map:
            normalized_fms_values.append(option_name)
            selected_fms_options.append(option)
    selected_fms_values = normalized_fms_values

    filter_column_definitions: list[dict[str, str]] = []
    filter_definitions_by_table: dict[str, list[dict[str, str]]] = {}
    for option in selected_fms_options:
        table_name = str(option.get("table_name") or "").strip()
        if not table_name:
            continue
        if table_name in filter_definitions_by_table:
            continue
        filter_definitions_by_table[table_name] = await metadata_service.get_dashboard_filter_columns(table_name)

    if selected_fms_options:
        seen_cols: set[str] = set()
        for defs in filter_definitions_by_table.values():
            for item in defs:
                col_name = str(item.get("column_name") or "").strip()
                if not col_name:
                    continue
                col_key = col_name.lower()
                if col_key in seen_cols:
                    continue
                seen_cols.add(col_key)
                filter_column_definitions.append(item)
    selected_fms_name = selected_fms_values[0] if selected_fms_values else ""
    multi_fms_mode = len(selected_fms_values) > 1

    # Section pagination uses p_<fms_key>; keep the data loader in sync with
    # that page so clicking Go/Next can fetch any already-background-loaded
    # page (or fetch that single page on demand if it is not cached yet).
    requested_data_page = max(int(page or 1), 1)
    if not multi_fms_mode and selected_fms_name:
        try:
            section_page_raw = request.query_params.get(
                f"p_{_section_key(selected_fms_name)}"
            )
            if section_page_raw:
                requested_data_page = max(int(str(section_page_raw).strip() or "1"), 1)
        except ValueError:
            requested_data_page = 1

    allowed_filter_columns = {
        str(item.get("column_name") or "").strip().lower()
        for item in filter_column_definitions
        if item.get("column_name")
    }
    fms_column_filters: dict[str, list[str]] = {}

    requested_doer_values = list(selected_doer_values)
    doer_options = await dashboard_service.get_doer_options(selected_fms_values)
    if multi_fms_mode:
        selected_doer_map = {v.lower(): v for v in selected_doer_values}
        selected_doer_values = [d for d in doer_options if d.lower() in selected_doer_map]
    else:
        selected_doer_values = requested_doer_values

    # Extract raw column filters before loading data so the data service can
    # decide whether it must scan every backend page. Multi-FMS filters are
    # section-scoped and are applied below, so any active section filter also
    # requires a complete source scan.
    if multi_fms_mode:
        fms_column_filters = {}
        has_section_filter = any(
            key.startswith("s_")
            and ("_ff_" in key or key.endswith("_status") or key.endswith("_search") or key.endswith("_doer"))
            for key in request.query_params.keys()
        )
    else:
        fms_column_filters = _extract_fms_filters(request, allowed_filter_columns)
        has_section_filter = False

    force_full_scan = bool(
        status
        or search
        or selected_doer_values
        or fms_column_filters
        or has_section_filter
    )

    stage_headers = await dashboard_service.get_stage_headers(selected_fms_name)
    if multi_fms_mode:
        all_tasks = await dashboard_service.get_dashboard_rows(
            fms_names=selected_fms_values,
            status=None,
            doers=None,
            search_text=None,
            fms_column_filters=None,
            include_cached_pages=progressive_refresh,
            requested_page=requested_data_page,
            force_full_scan=force_full_scan,
        )
    else:
        all_tasks = await dashboard_service.get_dashboard_rows(
            fms_names=selected_fms_values,
            status=status,
            doers=selected_doer_values,
            search_text=search,
            fms_column_filters=fms_column_filters,
            include_cached_pages=progressive_refresh,
            requested_page=requested_data_page,
            force_full_scan=force_full_scan,
        )

    # Filter dropdowns are populated from the COMPLETE source table. The grid can
    # remain progressive, but Sales Order (and every other configured filter) must
    # include values that live on page 3, 50, 100, etc.
    fms_filter_options = await dashboard_service.get_dashboard_filter_options_all_pages(
        selected_fms_options,
        filter_column_definitions,
    )
    for filter_def in fms_filter_options:
        selected_values = fms_column_filters.get(filter_def["column_name"], [])
        option_values = list(filter_def.get("options") or [])
        for selected_value in selected_values:
            if selected_value not in option_values:
                option_values.append(selected_value)
        filter_def["options"] = sorted(option_values, key=lambda item: str(item).lower())
        filter_def["selected_values"] = selected_values

    filter_query_pairs: list[tuple[str, str]] = []
    if multi_fms_mode:
        filter_query_pairs = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if str(value).strip()
        ]
        if not any(key == "fms_name" for key, _ in filter_query_pairs):
            for fms_val in selected_fms_values:
                filter_query_pairs.append(("fms_name", fms_val))
    else:
        for fms_val in selected_fms_values:
            filter_query_pairs.append(("fms_name", fms_val))
        if status:
            filter_query_pairs.append(("status", status))
        for doer_val in selected_doer_values:
            filter_query_pairs.append(("doer", doer_val))
        if search:
            filter_query_pairs.append(("search", search))
        for column_name, values in fms_column_filters.items():
            for value in values:
                filter_query_pairs.append((f"ff_{column_name}", value))
    page = max(page, 1)
    total = len(all_tasks)
    if not multi_fms_mode:
        result_total_pages = getattr(all_tasks, "total_pages_by_fms", {}).get(
            str(selected_fms_name or "").strip().lower()
        )
        total_pages = max(int(result_total_pages or 1), 1)
        page = min(requested_data_page, total_pages)
        requested_map = getattr(all_tasks, "requested_page_tasks_by_fms", {})
        tasks = list(
            requested_map.get(str(selected_fms_name or "").strip().lower(), [])
        )
    else:
        total_pages = 1
        page = 1
        tasks = all_tasks

    active_fms_name = selected_fms_name or "All FMS"
    if len(selected_fms_values) > 1:
        active_fms_name = ", ".join(selected_fms_values)

    selected_option_map = {
        str(opt.get("fms_name") or "").strip().lower(): opt
        for opt in selected_fms_options
        if str(opt.get("fms_name") or "").strip()
    }

    dashboard_sections: list[dict[str, Any]] = []
    section_fms_values = selected_fms_values or ([selected_fms_name] if selected_fms_name else [])
    for fms_value in section_fms_values:
        normalized_fms = str(fms_value or "").strip()
        if not normalized_fms:
            continue

        section_key = _section_key(normalized_fms)

        if not multi_fms_mode:
            # With an active filter the service performs a complete source scan.
            # Do NOT start section filtering from the backend page slice, or the
            # result would again be limited to page 1. Paginate only after all
            # requested filters have been applied.
            if getattr(all_tasks, "complete_scan", False):
                section_base_tasks = [
                    t for t in all_tasks
                    if str(getattr(t, "application_name", "") or "").strip().lower() == normalized_fms.lower()
                ]
            else:
                requested_map = getattr(all_tasks, "requested_page_tasks_by_fms", {})
                section_base_tasks = list(
                    requested_map.get(normalized_fms.lower(), [])
                )
        else:
            section_base_tasks = [
                t for t in all_tasks
                if str(getattr(t, "application_name", "") or "").strip().lower() == normalized_fms.lower()
            ]
        section_tasks = list(section_base_tasks)
        section_stage_headers = await dashboard_service.get_stage_headers(normalized_fms)
        section_doer_options: list[str] = []

        section_filters: list[str] = []
        section_status = ""
        section_search = ""
        section_doer_values: list[str] = []

        section_option = selected_option_map.get(normalized_fms.lower())
        section_table_name = str((section_option or {}).get("table_name") or "").strip()
        section_filter_defs = filter_definitions_by_table.get(section_table_name) or []
        section_allowed_filter_columns = {
            str(item.get("column_name") or "").strip().lower()
            for item in section_filter_defs
            if item.get("column_name")
        }
        section_column_filters: dict[str, list[str]] = {}

        if multi_fms_mode:
            section_status = str(request.query_params.get(f"s_{section_key}_status") or "").strip()
            section_search = str(request.query_params.get(f"s_{section_key}_search") or "").strip()
            raw_section_doers = [
                str(v).strip()
                for v in request.query_params.getlist(f"s_{section_key}_doer")
                if str(v).strip()
            ]
            section_doer_values = raw_section_doers
            section_column_filters = _extract_section_fms_filters(
                request,
                f"s_{section_key}",
                section_allowed_filter_columns,
            )
        else:
            section_status = str(status or "").strip()
            section_search = str(search or "").strip()
            section_doer_values = list(selected_doer_values)
            section_column_filters = dict(fms_column_filters)

        def _apply_section_filters(
            tasks_list: list[Any],
            *,
            skip_status: bool = False,
            skip_doer: bool = False,
            skip_search: bool = False,
            skip_column: str | None = None,
        ) -> list[Any]:
            filtered = list(tasks_list)

            if section_status and not skip_status:
                filtered = [
                    t for t in filtered
                    if str(getattr(t, "status", "") or "").strip().lower() == section_status.lower()
                ]

            if section_doer_values and not skip_doer:
                selected_doer_set = {str(d).strip().lower() for d in section_doer_values if str(d).strip()}
                filtered = [
                    t for t in filtered
                    if str(getattr(t, "assigned_doer", "") or "").strip().lower() in selected_doer_set
                ]

            if section_search and not skip_search:
                filtered = [t for t in filtered if _task_matches_search(t, section_search)]

            skip_col_key = str(skip_column or "").strip().lower()
            for col_name, values in section_column_filters.items():
                if skip_col_key and col_name.lower() == skip_col_key:
                    continue
                active_values = [v for v in values if str(v).strip()]
                if not active_values:
                    continue
                filtered = [
                    t for t in filtered
                    if _value_matches_any(_row_value_ci(getattr(t, "raw", {}) or {}, col_name), active_values)
                ]

            return filtered

        section_tasks = _apply_section_filters(section_base_tasks)

        fms_doer_options = await dashboard_service.get_doer_options([normalized_fms])
        section_doer_values_map = {
            str(v).strip().lower(): str(v).strip()
            for v in section_doer_values
            if str(v).strip()
        }
        doer_values: dict[str, str] = {}
        for doer_name_raw in fms_doer_options:
            doer_name = str(doer_name_raw or "").strip()
            if not doer_name:
                continue
            key = doer_name.lower()
            if key not in doer_values:
                doer_values[key] = doer_name
        for key, value in section_doer_values_map.items():
            if key not in doer_values:
                doer_values[key] = value
        section_doer_options = sorted(doer_values.values(), key=lambda item: item.lower())

        if section_status:
            section_filters.append(f"Status: {section_status}")
        if section_doer_values:
            section_filters.append(f"Doer: {', '.join(section_doer_values)}")
        if section_search:
            section_filters.append(f"Task ID: {section_search}")

        if section_table_name:
            section_filter_labels = {
                str(item.get("column_name") or ""): str(item.get("label") or item.get("column_name") or "")
                for item in section_filter_defs
                if item.get("column_name")
            }
            for col_name, values in section_column_filters.items():
                if col_name in section_filter_labels and values:
                    section_filters.append(f"{section_filter_labels[col_name]}: {', '.join(values)}")

        section_filter_options: list[dict[str, Any]] = []
        section_filter_columns: list[str] = []
        if section_table_name:
            # IMPORTANT: filter dropdown options must come from the complete
            # source table, not from section_base_tasks. section_base_tasks is
            # intentionally page-scoped during normal dashboard loading, so
            # using it here made the dropdown contain only values from the
            # currently visible page. The service helper walks every backend
            # page and caches the distinct values per table/column.
            complete_filter_options = await dashboard_service.get_dashboard_filter_options_all_pages(
                [section_option] if section_option else [],
                section_filter_defs,
            )
            complete_options_by_column = {
                str(item.get("column_name") or "").strip().lower(): list(item.get("options") or [])
                for item in complete_filter_options
                if item.get("column_name")
            }

            for filter_def in section_filter_defs:
                col_name = str(filter_def.get("column_name") or "").strip()
                if not col_name:
                    continue
                section_filter_columns.append(col_name)

                selected_values = list(section_column_filters.get(col_name, []))
                option_values = list(complete_options_by_column.get(col_name.lower(), []))

                # Keep an explicitly selected value visible even if the source
                # changed between the dropdown-load and this request.
                existing_keys = {str(v).strip().casefold() for v in option_values}
                for selected_value in selected_values:
                    key = str(selected_value).strip().casefold()
                    if key and key not in existing_keys:
                        option_values.append(selected_value)
                        existing_keys.add(key)

                option_values = sorted(
                    {str(v).strip(): None for v in option_values if str(v).strip()}.keys(),
                    key=lambda item: item.casefold(),
                )

                section_filter_options.append({
                    "column_name": col_name,
                    "label": str(filter_def.get("label") or col_name),
                    "options": option_values,
                    "selected_values": selected_values,
                })

        dashboard_sections.append({
            "section_key": section_key,
            "fms_name": normalized_fms,
            "tasks": section_tasks,
            "stage_headers": section_stage_headers,
            "status": section_status,
            "search": section_search,
            "selected_doer_values": section_doer_values,
            "doer_options": section_doer_options,
            "applied_filters": section_filters,
            "filter_options": section_filter_options,
            "filter_reset_query": urlencode(
                (
                    [
                        (key, value)
                        for key, value in filter_query_pairs
                        if key not in {
                            f"s_{section_key}_status",
                            f"s_{section_key}_search",
                            f"s_{section_key}_doer",
                            *{f"s_{section_key}_ff_{col}" for col in section_filter_columns},
                        }
                    ]
                    if multi_fms_mode
                    else [
                        (key, value)
                        for key, value in filter_query_pairs
                        if key not in {f"ff_{col}" for col in section_filter_columns}
                    ]
                ),
                doseq=True,
            ),
        })

    if not multi_fms_mode and dashboard_sections:
        primary_section = dashboard_sections[0]
        doer_options = list(primary_section.get("doer_options") or [])
        selected_doer_values = list(primary_section.get("selected_doer_values") or selected_doer_values)

    if not dashboard_sections:
        dashboard_sections = [{
            "fms_name": selected_fms_name or "FMS",
            "tasks": all_tasks,
            "stage_headers": stage_headers,
            "applied_filters": [],
            "filter_options": fms_filter_options,
        }]

    for section in dashboard_sections:
        section_name = str(section.get("fms_name") or "")
        section_key = _section_key(section_name)
        page_param = f"p_{section_key}"

        try:
            requested_section_page = int(str(request.query_params.get(page_param) or "1").strip() or "1")
        except ValueError:
            requested_section_page = 1
        requested_section_page = max(requested_section_page, 1)

        section_all_tasks = list(section.get("tasks") or [])
        section_total = len(section_all_tasks)
        if not multi_fms_mode and getattr(all_tasks, "complete_scan", False):
            # Complete scans contain the entire filtered dataset. Pagination is
            # therefore calculated from the filtered count, not the SQL page.
            section_total_pages = max((section_total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
            section_page = min(requested_section_page, section_total_pages)
            start = (section_page - 1) * PAGE_SIZE
            section_tasks = section_all_tasks[start:start + PAGE_SIZE]
            section_total = len(section_all_tasks)
        elif not multi_fms_mode:
            result_total_pages = getattr(all_tasks, "total_pages_by_fms", {}).get(
                normalized_fms.lower()
            )
            section_total_pages = max(int(result_total_pages or 1), 1)
            section_page = min(requested_section_page, section_total_pages)
            # section_base_tasks is already the exact requested data page.
            section_tasks = section_all_tasks
        else:
            section_total_pages = max((section_total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
            section_page = min(requested_section_page, section_total_pages)
            start = (section_page - 1) * PAGE_SIZE
            section_tasks = section_all_tasks[start:start + PAGE_SIZE]

        section["tasks"] = section_tasks
        section["page_param"] = page_param
        section["page"] = section_page
        section["total"] = section_total
        section["total_pages"] = section_total_pages
        section["show_pager"] = section_total_pages > 1
        section["pager_query_prev"] = _build_query_string(filter_query_pairs, {page_param: str(max(section_page - 1, 1))})
        section["pager_query_next"] = _build_query_string(filter_query_pairs, {page_param: str(min(section_page + 1, section_total_pages))})

        hidden_pairs = [
            (key, value)
            for key, value in filter_query_pairs
            if key != page_param
        ]
        section["pager_hidden_pairs"] = hidden_pairs

    return {
        "request": request,
        "fms_options": fms_options,
        "doer_options": doer_options,
        "stage_headers": stage_headers,
        "statuses": STATUSES,
        "tasks": tasks,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "selected_fms_name": selected_fms_name,
        "selected_fms_label": selected_fms_name or "FMS",
        "active_fms_name": active_fms_name,
        "selected_fms_values": selected_fms_values,
        "selected_doer_values": selected_doer_values,
        "current_time": datetime.now().strftime("%m/%d/%Y, %I:%M:%S %p"),
        "fms_filter_options": fms_filter_options,
        "fms_column_filters": fms_column_filters,
        "filter_query_pairs": filter_query_pairs,
        "filter_query_string": urlencode(filter_query_pairs, doseq=True),
        "dashboard_sections": dashboard_sections,
        "multi_fms_mode": multi_fms_mode,
        "progressive_dashboard_loading": dashboard_service.is_progressive_dashboard_loading(),
        "filters": {
            "fms_name": selected_fms_name,
            "status": status or "",
            "doer": selected_doer_values[0] if selected_doer_values else "",
            "search": search or "",
        },
    }


@router.get("/dashboard/busy-pickers", response_class=JSONResponse)
async def dashboard_busy_pickers():
    """Return all unfinished Stage 1 picker assignments for the dashboard board."""
    try:
        assignments = await stage1_custom_service.get_busy_picker_assignments("")
    except Exception as exc:
        print(f"[Dashboard Busy Picker] Failed to load assignments: {exc}")
        return JSONResponse(
            {"ok": False, "error": "Unable to load busy picker board."},
            status_code=502,
        )

    rows: list[dict[str, Any]] = []
    busy_names: list[str] = []
    seen_names: set[str] = set()

    for assignment in assignments or []:
        row_links = [
            {
                "row_id": row_id,
                "stage_url": route_builder.stage_form_route(
                    str(row_id), "S1", "Z_SalesOrder", "RowID"
                ),
            }
            for row_id in (assignment.get("row_ids") or [])
        ]
        pickers = list(assignment.get("pickers") or [])
        rows.append({
            "pick_id": assignment.get("pick_id") or "-",
            "pickers": pickers,
            "row_links": row_links,
        })
        for picker in pickers:
            key = str(picker or "").strip().casefold()
            if key and key not in seen_names:
                seen_names.add(key)
                busy_names.append(str(picker).strip())

    busy_names.sort(key=str.casefold)
    return JSONResponse({
        "ok": True,
        "busy_picker_names": busy_names,
        "assignments": rows,
    })


def _r360_dashboard_value(row: dict[str, Any], *names: str) -> Any:
    """Read a Runner360 value using case- and punctuation-insensitive aliases."""
    wanted = {re.sub(r"[^a-z0-9]", "", name.casefold()) for name in names}
    for key, value in row.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if normalized in wanted and value not in (None, ""):
            return value
    return None


def _r360_sunday_start(value: date) -> date:
    """Return the Sunday on or before a date."""
    return value - timedelta(days=(value.weekday() + 1) % 7)


def _r360_weeks_for_year(year: int) -> list[dict[str, Any]]:
    """Create Sunday-Saturday weeks overlapping the selected year."""
    first_day = date(year, 1, 1)
    last_day = date(year, 12, 31)
    week_start = _r360_sunday_start(first_day)
    options: list[dict[str, Any]] = []
    number = 1
    while week_start <= last_day:
        week_end = week_start + timedelta(days=6)
        options.append({
            "key": f"{year}-W{number:02d}",
            "year": year,
            "number": number,
            "start": week_start,
            "end": week_end,
            "label": f"Week {number:02d} — {week_start.strftime('%d %b %Y')} to {week_end.strftime('%d %b %Y')}",
        })
        week_start += timedelta(days=7)
        number += 1
    return options


@router.get("/runner360/dashboard", response_class=HTMLResponse)
async def runner360_analytics_dashboard(
    request: Request,
    group_by: str = Query("day", pattern="^(day|week)$"),
    selected_date: str | None = Query(None),
):
    """Show actual BusyTimeMins by runner, day, and selected Sunday-Saturday weeks.

    Runner options come from active employee records associated with a Runner team,
    not only from Z_Runner360 rows. This ensures runners with no assigned task yet
    are still available in the filter.
    """
    today = date.today()
    try:
        all_rows = await table_api.get_table("Z_Runner360")
    except Exception as exc:
        logging.exception("Unable to load Runner360 rows for analytics")
        raise HTTPException(status_code=502, detail="Unable to load Runner360 data for the dashboard.") from exc

    runner_names_from_rows: set[str] = set()
    task_records: list[dict[str, Any]] = []
    for row in all_rows or []:
        if not isinstance(row, dict):
            continue
        runner_name = str(_r360_dashboard_value(row, "I_Runner", "S1_Doer", "Runner", "RunnerName") or "").strip()
        if runner_name:
            runner_names_from_rows.add(runner_name)

        start_raw = _r360_dashboard_value(
            row, "S1_PLBaseTS", "S1_BaseTS", "I_TaskStartDateTime", "I_TaskDateTime"
        )
        if start_raw is None:
            task_date = _r360_dashboard_value(row, "I_TaskDate", "TaskDate")
            task_time = _r360_dashboard_value(row, "I_TaskStartTime", "TaskStartTime")
            if task_date and task_time:
                start_raw = f"{task_date} {task_time}"
        start_dt = parse_datetime(start_raw)
        if start_dt is None:
            continue

        status_value = str(_r360_dashboard_value(row, "S1_TaskStatus", "S1_Task_Status") or "").strip().casefold()
        if status_value in {"cancelled", "canceled", "revoked"}:
            continue

        actual_raw = _r360_dashboard_value(row, "S1_ALTimeStamp", "S1_ActualTimestamp", "S1_ActualTimeStamp")
        actual_dt = parse_datetime(actual_raw)
        busy_raw = _r360_dashboard_value(row, "BusyTimeMins", "Busy_Time_Mins", "Busy Time")
        busy_minutes: int | None = None
        if actual_dt is not None:
            try:
                if busy_raw is not None:
                    busy_minutes = max(0, int(float(str(busy_raw).strip())))
            except (TypeError, ValueError, OverflowError):
                busy_minutes = None
            if busy_minutes is None:
                busy_minutes = max(0, int((actual_dt - start_dt).total_seconds() // 60))

        task_records.append({
            "start_date": start_dt.date(),
            "runner": runner_name or "Unknown runner",
            "busy_minutes": busy_minutes,
            "measured": actual_dt is not None and busy_minutes is not None,
        })

    # Use the shared Runner360 assignment loader so all active people on teams
    # identified as Runner appear, even when their names are not in Z_Runner360 yet.
    master_runner_names: set[str] = set()
    try:
        from app.routers.runner360 import _get_runner_assignments
        assignments = await _get_runner_assignments()
        master_runner_names = {
            str(item.get("employee_name") or "").strip()
            for item in assignments
            if str(item.get("employee_name") or "").strip()
        }
    except Exception:
        logging.exception("Unable to load active Runner360 options from VM_Team/VM_EmployeeMaster")

    # Retain historical names so old tasks are still filterable if an employee is no
    # longer on the current Runner team. On success the employee master adds all current runners.
    runner_names_canonical: dict[str, str] = {}
    for raw_name in sorted(master_runner_names | runner_names_from_rows, key=str.casefold):
        runner_names_canonical.setdefault(raw_name.casefold(), raw_name)
    runner_options = sorted(runner_names_canonical.values(), key=str.casefold)
    # Use the canonical master spelling for record aggregation so differences in
    # casing between older task rows and the employee master do not split totals.
    for record in task_records:
        canonical_name = runner_names_canonical.get(str(record.get("runner") or "").casefold())
        if canonical_name:
            record["runner"] = canonical_name

    query = request.query_params
    runner_filter_set = "runner_filter_set" in query
    requested_runners = [str(v).strip() for v in query.getlist("runner") if str(v).strip()]
    valid_runner_map = {name.casefold(): name for name in runner_options}
    if runner_filter_set:
        selected_runners = list(dict.fromkeys(valid_runner_map[v.casefold()] for v in requested_runners if v.casefold() in valid_runner_map))
    else:
        # First view shows all active runners so the dashboard immediately compares them.
        selected_runners = list(runner_options)
    selected_runner_set = {v.casefold() for v in selected_runners}

    # Current year plus the following ten years are always available, even if no tasks
    # exist yet in those years. Dates can still be entered manually in Daily mode.
    year_options = list(range(today.year, today.year + 11))
    year_filter_set = "year_filter_set" in query
    requested_years: list[int] = []
    for raw_year in query.getlist("year"):
        try:
            requested_years.append(int(raw_year))
        except (TypeError, ValueError):
            continue
    valid_years = set(year_options)
    if year_filter_set:
        selected_years = list(dict.fromkeys(y for y in requested_years if y in valid_years))
    elif requested_years:
        selected_years = list(dict.fromkeys(y for y in requested_years if y in valid_years))
    else:
        selected_years = [today.year]

    all_week_options = [week for yr in year_options for week in _r360_weeks_for_year(yr)]
    week_by_key = {item["key"]: item for item in all_week_options}
    visible_week_options = [item for item in all_week_options if item["year"] in set(selected_years)]
    week_filter_set = "week_filter_set" in query
    requested_week_keys = [str(v).strip() for v in query.getlist("week_key") if str(v).strip()]

    # Backward compatibility with links generated by the previous single-week filter.
    if not requested_week_keys and query.get("week_no"):
        try:
            old_year = int(query.get("year") or today.year)
            old_week_number = int(query.get("week_no") or 1)
            old_match = next((w for w in all_week_options if w["year"] == old_year and w["number"] == old_week_number), None)
            if old_match:
                requested_week_keys = [old_match["key"]]
        except (TypeError, ValueError):
            pass

    allowed_week_keys = {item["key"] for item in visible_week_options}
    if week_filter_set:
        selected_week_keys = list(dict.fromkeys(k for k in requested_week_keys if k in allowed_week_keys))
    elif requested_week_keys:
        selected_week_keys = list(dict.fromkeys(k for k in requested_week_keys if k in allowed_week_keys))
    else:
        current_week_start = _r360_sunday_start(today)
        current_key = next((w["key"] for w in all_week_options if w["start"] == current_week_start), None)
        selected_week_keys = [current_key] if current_key and current_key in allowed_week_keys else ([visible_week_options[0]["key"]] if visible_week_options else [])

    selected_week_options = [week_by_key[key] for key in selected_week_keys if key in week_by_key]
    if group_by == "week":
        selected_dates: set[date] = set()
        for week in selected_week_options:
            cursor = week["start"]
            while cursor <= week["end"]:
                selected_dates.add(cursor)
                cursor += timedelta(days=1)
        ordered_dates = sorted(selected_dates)
        selected_date_value = today.isoformat()
        if selected_week_options:
            period_title = (
                selected_week_options[0]["label"] if len(selected_week_options) == 1
                else f"{len(selected_week_options)} selected weeks"
            )
        else:
            period_title = "No weeks selected"
    else:
        try:
            chosen_day = date.fromisoformat(str(selected_date or "").strip())
        except ValueError:
            chosen_day = today
        selected_date_value = chosen_day.isoformat()
        ordered_dates = [chosen_day]
        selected_dates = {chosen_day}
        period_title = chosen_day.strftime("%A, %d %B %Y")

    runner_records = [
        item for item in task_records
        if item["start_date"] in selected_dates
        and item["runner"].casefold() in selected_runner_set
    ]

    daily_runner_buckets: dict[tuple[date, str], dict[str, int]] = {}
    for day in ordered_dates:
        for runner_name in selected_runners:
            daily_runner_buckets[(day, runner_name)] = {"busy_minutes": 0, "completed_tasks": 0, "task_count": 0}

    for item in runner_records:
        day = item["start_date"]
        runner_name = item["runner"]
        bucket = daily_runner_buckets.setdefault((day, runner_name), {"busy_minutes": 0, "completed_tasks": 0, "task_count": 0})
        bucket["task_count"] += 1
        if item["measured"]:
            bucket["completed_tasks"] += 1
            bucket["busy_minutes"] += int(item["busy_minutes"] or 0)

    chart_labels = [day.strftime("%a %d %b") if group_by == "week" else day.strftime("%d %b %Y") for day in ordered_dates]
    chart_series: list[dict[str, Any]] = []
    for runner_name in selected_runners:
        chart_series.append({
            "name": runner_name,
            "values": [daily_runner_buckets.get((day, runner_name), {}).get("busy_minutes", 0) for day in ordered_dates],
        })
    # Flat values are retained for KPI/backward template compatibility: totals across all selected runners.
    chart_values = [
        sum(daily_runner_buckets.get((day, runner_name), {}).get("busy_minutes", 0) for runner_name in selected_runners)
        for day in ordered_dates
    ]

    chart_details: list[dict[str, Any]] = []
    for day in ordered_dates:
        for runner_name in selected_runners:
            bucket = daily_runner_buckets.get((day, runner_name), {"busy_minutes": 0, "completed_tasks": 0, "task_count": 0})
            chart_details.append({
                "date": day.strftime("%A, %d %B %Y"),
                "runner": runner_name,
                "busy_minutes": int(bucket["busy_minutes"]),
                "completed_tasks": int(bucket["completed_tasks"]),
                "task_count": int(bucket["task_count"]),
            })

    return templates.TemplateResponse(request, "dashboard/runner360_analytics.html", {
        "group_by": group_by,
        "selected_date": selected_date_value,
        "year_options": year_options,
        "selected_years": selected_years,
        "week_options": all_week_options,
        "selected_week_keys": selected_week_keys,
        "selected_week_options": selected_week_options,
        "selected_week_title": period_title,
        "selected_runners": selected_runners,
        "runner_options": runner_options,
        "chart_labels": chart_labels if selected_runners else [],
        "chart_values": chart_values if selected_runners else [],
        "chart_series": chart_series if selected_runners else [],
        "chart_details": chart_details if selected_runners else [],
        "total_busy_minutes": sum(chart_values) if selected_runners else 0,
        "completed_tasks": sum(item["completed_tasks"] for item in chart_details) if selected_runners else 0,
        "tasks_in_range": sum(item["task_count"] for item in chart_details) if selected_runners else 0,
        "master_runner_list_loaded": bool(master_runner_names),
        "current_time": datetime.now().strftime("%d-%m-%Y %H:%M"),
    })


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    fms_name: str | None = Query(None),
    status: str | None = Query(None),
    doer: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1),
):
    context = await _dashboard_context(request, fms_name, status, doer, search, page)
    return templates.TemplateResponse(request, "dashboard/index.html", context)


@router.get("/dashboard/rows", response_class=HTMLResponse)
async def dashboard_rows_partial(
    request: Request,
    fms_name: str | None = Query(None),
    status: str | None = Query(None),
    doer: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1),
):
    """HTMX partial - re-rendered on every filter change without a full reload."""
    context = await _dashboard_context(request, fms_name, status, doer, search, page)
    return templates.TemplateResponse(request, "dashboard/_table.html", context)


@router.get("/tasks", response_class=HTMLResponse)
async def all_tasks_page(
    request: Request,
    app_id: str | None = Query(None),
    status: str | None = Query(None),
    doer: str | None = Query(None),
    so_no: str | None = Query(None),
    po_no: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1),
):
    redirect_url = request.url_for("dashboard_page")
    query = request.url.query
    if query:
        redirect_url = f"{redirect_url}?{query}"
    return RedirectResponse(url=redirect_url, status_code=307)


@router.get("/mytasks", response_class=HTMLResponse)
async def my_tasks_page(
    request: Request,
    doer: str | None = Query(None),
    status: str | None = Query(None),
    so_no: str | None = Query(None),
    po_no: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1),
):
    redirect_url = request.url_for("dashboard_page")
    query = request.url.query
    if query:
        redirect_url = f"{redirect_url}?{query}"
    return RedirectResponse(url=redirect_url, status_code=307)


@router.get("/dashboard/modal/{task_id}", response_class=HTMLResponse)
async def dashboard_modal(
    request: Request,
    task_id: str,
    table_name: str = "",
    app_id: str = "",
    primary_key: str = "",
):
    """Render details for the exact dashboard FMS row, even when IDs overlap."""
    context = await task_service.get_task_context(
        task_id,
        table_name=(table_name.strip() or None),
        app_id=(app_id.strip() or None),
        primary_key=(primary_key.strip() or None),
    )
    sections = await form_service.build_dashboard_modal_sections(
        context["fms"]["table_name"],
        context["row"],
        task_id,
        context["primary_key"],
    )
    
    return templates.TemplateResponse(request, "dashboard/_modal.html", {
        "task_id": task_id,
        "context": context,
        "sections": sections
    })
