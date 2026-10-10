"""
Single source of truth for every URL in the portal.

Every link, redirect, deep-link and QR code MUST be built through this
module - no template or router should ever concatenate a path by hand.
This is what makes the Developer Hub's Route Generator possible: it just
calls these same functions.
"""
from __future__ import annotations

from urllib.parse import quote_plus

from app.config import settings

# Internal (relative) routes -------------------------------------------------


def dashboard_route() -> str:
    return "/dashboard"


def my_tasks_route() -> str:
    return "/mytasks"


def all_tasks_route() -> str:
    return "/tasks"


def task_route(task_id: str) -> str:
    return f"/task/{task_id}"


def task_timeline_route(task_id: str) -> str:
    return f"/task/{task_id}/timeline"


def task_history_route(task_id: str) -> str:
    return f"/task/{task_id}/history"


def task_metadata_route(task_id: str) -> str:
    return f"/task/{task_id}/metadata"


def application_form_route(task_id: str) -> str:
    return f"/forms/application/{task_id}"


def stage_form_route(
    task_id: str,
    stage_no: str,
    table_name: str = "",
    primary_key: str = "",
    app_id: str = "",
) -> str:
    url = f"/forms/stage/{task_id}/{stage_no}"
    params = []
    if table_name:
        params.append(f"table_name={quote_plus(str(table_name))}")
    if primary_key:
        params.append(f"primary_key={quote_plus(str(primary_key))}")
    if app_id:
        params.append(f"app_id={quote_plus(str(app_id))}")
    return url + ("?" + "&".join(params) if params else "")


def stage_sort_key(stage_no: str):
    """Return a stable natural sort key for stage identifiers such as S1, S2, S10."""
    value = str(stage_no or "").strip().upper()
    import re
    match = re.fullmatch(r"S(\d+)", value)
    if match:
        return (0, int(match.group(1)))
    return (1, value)


def stage_lead_time_grant_route(task_id: str, stage_no: str) -> str:
    return f"/forms/stage/{task_id}/{stage_no}/lead-time-grant"


def stage_clear_route(task_id: str, stage_no: str) -> str:
    return f"/forms/stage/{task_id}/{stage_no}/clear-responses"


def developer_hub_route() -> str:
    return "/developer"


def developer_apis_route() -> str:
    return "/developer/apis"


def developer_routes_route() -> str:
    return "/developer/routes"


def developer_deeplinks_route() -> str:
    return "/developer/deeplinks"


def developer_metadata_route() -> str:
    return "/developer/metadata"


def developer_form_preview_route() -> str:
    return "/developer/forms"


# Absolute URL helper (for QR codes / external systems e.g. SAP) ------------


def absolute_url(relative_route: str) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}{relative_route}"


def build_task_route_map(task_id: str, stage_numbers: list[str], current_stage_no: str | None = None) -> dict:
    """Every route addressable for a single task - powers the Route Generator."""
    routes = {
        "Task": task_route(task_id),
        "Timeline": task_timeline_route(task_id),
        "History": task_history_route(task_id),
        "Application Form": application_form_route(task_id),
        "Application Metadata": task_metadata_route(task_id),
    }
    if current_stage_no:
        routes["Current Stage"] = stage_form_route(task_id, current_stage_no)
    for stage_no in stage_numbers:
        routes[f"Stage {stage_no}"] = stage_form_route(task_id, stage_no)
    return routes


def developer_custom_forms_route() -> str:
    return "/developer/custom-forms"
