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
from app.utils.status_calculator import STATUS_COLORS

templates = Jinja2Templates(directory="app/templates")

templates.env.globals["routes"] = route_builder
templates.env.globals["status_colors"] = STATUS_COLORS
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["environment"] = settings.environment
templates.env.globals["is_production"] = settings.is_production
templates.env.globals["comments_origin"] = settings.comments_origin.rstrip("/")
templates.env.globals["one_portal_origin"] = settings.one_portal_origin.rstrip("/")
templates.env.globals["current_role_id"] = 1  # Simulated Admin Role for Eye Button
templates.env.filters["duration"] = format_duration
