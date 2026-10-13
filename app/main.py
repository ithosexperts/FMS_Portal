
"""
FMS Portal - FastAPI application entrypoint.

This portal is a pure API consumer: it renders UI and calls out to the
shared API_1_URL / API_2_URL services. It never talks to SQL directly and
never runs its own backend logic beyond metadata-driven computation
(current stage / status / delay), all of which lives under app/utils.
"""
from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.client import ApiError, api_client
from app.config import settings
from app.middleware.oneportal_auth import OnePortalAuthMiddleware
from app.routers import dashboard, developer, forms, misc, sso, task
from app.services.task_service import TaskNotFoundError
from app.services.stage_service import StageNotFoundError
from app.templates_config import templates

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reuse one HTTP connection pool for the complete portal lifetime.
    # The portal makes many small API calls while rendering forms/dashboard.
    yield
    await api_client.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(OnePortalAuthMiddleware)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(sso.router)
app.include_router(dashboard.router)
app.include_router(task.router)
app.include_router(forms.router)
app.include_router(developer.router)
app.include_router(misc.router)


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/healthz")
async def health_check():
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


@app.exception_handler(TaskNotFoundError)
async def task_not_found_handler(request: Request, exc: TaskNotFoundError):
    return templates.TemplateResponse(
        request, "errors/404.html", {"message": str(exc)}, status_code=404,
    )


@app.exception_handler(StageNotFoundError)
async def stage_not_found_handler(request: Request, exc: StageNotFoundError):
    return templates.TemplateResponse(
        request, "errors/404.html", {"message": str(exc)}, status_code=404,
    )


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return templates.TemplateResponse(
        request,
        "errors/500.html",
        {"message": str(exc), "status_code": exc.status_code},
        status_code=502,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Never leak Starlette's bare 'Internal Server Error' into a form modal.

    Log the complete traceback, then render the portal's own 500 page. A final
    HTML fallback is kept in case the error template itself cannot be rendered.
    """
    logging.exception("Unhandled error on %s", request.url)
    # During local development show the real exception in the form modal/page
    # instead of hiding it behind a generic 500. Production keeps the generic
    # message so internal details are not exposed to users.
    environment = str(getattr(settings, "environment", "")).strip().lower()
    message = (
        f"{type(exc).__name__}: {exc}"
        if environment not in {"production", "prod", "live"}
        else "Something went wrong while loading this page."
    )
    try:
        return templates.TemplateResponse(
            request,
            "errors/500.html",
            {"message": message, "status_code": 500},
            status_code=500,
        )
    except Exception:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<html><body style='font-family:Arial;padding:32px'>"
            "<h2>Unable to load this page</h2>"
            "<p>Please refresh the page. The server error has been logged.</p>"
            "</body></html>",
            status_code=500,
        )



