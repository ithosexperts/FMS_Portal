"""CEOITBOX WhatsApp notification integration for Runner360.

Uses the company's documented GET /api/v1/message/create endpoint. Query
parameters are passed separately so httpx URL-encodes special characters.
The WhatsApp notification is best-effort: a provider failure must never make a
successfully-created FMS task look as though it failed to save.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("fms_portal.whatsapp")


def _normalise_mobile(value: Any) -> str:
    """Return an international mobile number (India default for 10 digits).

    API/SQL gateways sometimes serialize a numeric mobile column as a float-like
    string, e.g. ``7065376766.0``. Strip only a zero-only decimal suffix before
    digit validation. Ordinary integers and strings continue to work unchanged.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    # Accept numeric mobile values serialized as 7065376766.0 or +917065376766.0.
    # Do not turn arbitrary decimals into phone numbers; only discard a suffix
    # made entirely of zeroes.
    if re.fullmatch(r"\+?\d+\.0+", raw):
        raw = raw.split(".", 1)[0]

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if 11 <= len(digits) <= 15 and raw.startswith("+"):
        return f"+{digits}"
    return ""


def _task_message(
    *,
    task_id: str,
    runner: str,
    branch: str,
    purpose: str,
    task_start_datetime: str,
    expected_return_time: str,
    lead_time_mins: int,
    test_mode: bool,
    checkin_link: str = "",
) -> str:
    prefix = "[TEST] " if test_mode else ""
    return "\n".join(
        [
            f"{prefix}Runner360 task assignment",
            f"Task ID: {task_id}",
            f"Runner: {runner}",
            f"Branch: {branch}",
            f"Purpose: {purpose}",
            f"Planned start: {task_start_datetime}",
            f"Expected return: {expected_return_time}",
            f"Planned duration: {lead_time_mins} minutes",
            "",
            f"Mobile Check In: {checkin_link}" if checkin_link else "Mobile Check In link unavailable. Please contact your FMS administrator.",
        ]
    )


def _result(*, attempted: bool, sent: bool, message: str, mode: str, provider_message_id: str = "") -> dict[str, Any]:
    return {
        "attempted": attempted,
        "sent": sent,
        "message": message,
        "mode": mode,
        "provider_message_id": provider_message_id,
    }


async def send_runner360_task_notification(
    *,
    task_id: str,
    runner: str,
    branch: str,
    purpose: str,
    task_start_datetime: str,
    expected_return_time: str,
