"""
Small standalone routes referenced by the sidebar: global Search, a quick
Timeline lookup, and a Settings placeholder (dark mode + environment info).
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.services import task_service
from app.templates_config import templates
from app.utils import route_builder

router = APIRouter(tags=["misc"])


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str | None = Query(None)):
    if q:
        try:
            await task_service.get_task_context(q)
            return RedirectResponse(route_builder.task_route(q))
        except task_service.TaskNotFoundError:
            return RedirectResponse(f"{route_builder.all_tasks_route()}?search={q}")
    return templates.TemplateResponse(request, "misc/search.html", {})


@router.get("/timeline", response_class=HTMLResponse)
async def timeline_lookup(request: Request, task_id: str | None = Query(None)):
    if task_id:
        return RedirectResponse(route_builder.task_timeline_route(task_id))
    return templates.TemplateResponse(request, "misc/timeline_lookup.html", {})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "misc/settings.html", {
        "api_1_configured": bool(settings.api_1_url),
        "api_2_configured": bool(settings.api_2_url),
    })
