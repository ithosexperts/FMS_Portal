"""
Task Details / Timeline / History / Metadata routes.

/task/{task_id}                -> Application Details + Timeline + Current Stage form
/task/{task_id}/timeline        -> Timeline-only view
/task/{task_id}/history         -> Completed stage history
/task/{task_id}/metadata        -> Column metadata of this task's table
"""
from __future__ import annotations

import os
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services import form_service, stage_service, task_service
from app.templates_config import templates

router = APIRouter(tags=["task"])


@router.get("/task/{task_id}", response_class=HTMLResponse)
async def task_details(
    request: Request,
    task_id: str,
    table_name: str | None = Query(None),
    app_id: str | None = Query(None),
    primary_key: str | None = Query(None),
):
    context = await task_service.get_task_context(
        task_id,
        table_name=table_name,
        app_id=app_id,
        primary_key=primary_key,
    )
    table_name = context["fms"]["table_name"]
    application_fields = await form_service.build_application_form(table_name, context["row"])

    stage_context = None
    if context["current_stage"]:
        stage_context = await stage_service.get_stage_context(task_id, context["current_stage"]["STAGE_NO"])

    # Auto-detect Custom Form template
    custom_template_path = f"custom_forms/{table_name}.html"
    full_path = os.path.join("app", "templates", "custom_forms", f"{table_name}.html")
    has_custom_form = os.path.exists(full_path)

    return templates.TemplateResponse(request, "task/details.html", {
        "task_id": task_id,
        "context": context,
        "application_fields": application_fields,
        "stage_context": stage_context,
        "custom_form_template": custom_template_path if has_custom_form else None,
    })


@router.get("/task/{task_id}/timeline", response_class=HTMLResponse)
async def task_timeline(request: Request, task_id: str):
    context = await task_service.get_task_context(task_id)
    return templates.TemplateResponse(request, "task/timeline.html", {
        "task_id": task_id, "context": context,
    })


@router.get("/task/{task_id}/history", response_class=HTMLResponse)
async def task_history(request: Request, task_id: str):
    context = await task_service.get_task_context(task_id)
    completed_stages = [s for s in context["timeline"] if s["status"] == "Completed"]
    return templates.TemplateResponse(request, "task/history.html", {
        "task_id": task_id, "context": context, "completed_stages": completed_stages,
    })


@router.get("/task/{task_id}/metadata", response_class=HTMLResponse)
async def task_metadata(request: Request, task_id: str):
    context = await task_service.get_task_context(task_id)
    app_id = context["fms"].get("app_id")
    target = f"/developer/metadata?app_id={app_id}&task_id={task_id}"
    return RedirectResponse(url=target, status_code=303)