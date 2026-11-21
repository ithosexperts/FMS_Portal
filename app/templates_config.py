"""
Single shared Jinja2Templates instance + template-global helpers.

Every router imports `templates` from here - never instantiates its own,
so global functions (route builders, status colours, ...) are available
in every template without repeating imports.
"""
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.utils import route_builder
from app.utils.delay_calculator import format_duration
from app.utils.permission_helper import can_access_stage
from app.utils.status_calculator import STATUS_COLORS

templates = Jinja2Templates(directory="app/templates")

templates.env.globals["routes"] = route_builder
templates.env.globals["status_colors"] = STATUS_COLORS
templates.env.globals["can_access_stage"] = can_access_stage
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["environment"] = settings.environment
templates.env.globals["is_production"] = settings.is_production
templates.env.globals["comments_origin"] = settings.comments_origin.rstrip("/")
templates.env.globals["one_portal_origin"] = settings.one_portal_origin.rstrip("/")
templates.env.globals["one_portal_app_window_url"] = settings.one_portal_app_window_url.rstrip("/")
templates.env.globals["cases_system_app_id_production"] = settings.cases_system_app_id_production
templates.env.globals["cases_system_app_id_test"] = settings.cases_system_app_id_test
templates.env.globals["get_cases_system_app_id"] = settings.get_cases_system_app_id
templates.env.globals["current_role_id"] = ""  # Default empty; actual role_id supplied from request context
templates.env.filters["duration"] = format_duration
