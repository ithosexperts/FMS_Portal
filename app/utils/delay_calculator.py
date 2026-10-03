"""
Delay calculation - the amount of time a stage is/was overdue.

A stage is only "late" once its Planned time has passed without an Actual
being recorded, or when the Actual was recorded after the Planned time.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I:%M:%S %p",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %I:%M %p",
    "%d-%m-%Y %I:%M:%S %p",
    "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %I:%M %p",
    "%d/%m/%Y %I:%M:%S %p",
    "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y",
)


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort parse of a date/datetime value coming back from the API."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def calculate_delay_minutes(planned: Any, actual: Any, now: datetime | None = None) -> int:
    """
    Minutes a stage is/was delayed.

    - Not yet actioned (actual blank): delay = now - planned (0 if not yet due).
    - Already actioned: delay = actual - planned (0 if completed on/before time).
    """
    planned_dt = parse_datetime(planned)
    if planned_dt is None:
        return 0

    now = now or datetime.now()
    reference = parse_datetime(actual) or now
    delta_minutes = int((reference - planned_dt).total_seconds() // 60)
    return max(delta_minutes, 0)


def format_duration(minutes: int) -> str:
    """Render a minute count as e.g. '2d 3h 15m', '4h 5m', '12m'."""
    if minutes <= 0:
        return "-"
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)
