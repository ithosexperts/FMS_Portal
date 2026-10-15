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
    """Return an international mobile number (India default for 10 digits)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
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
            "The mobile Check In link will be enabled after deployment.",
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
    lead_time_mins: int,
    runner_mobile: str = "",
) -> dict[str, Any]:
    """Send a task assignment to the configured test number or runner number."""
    test_mode = bool(settings.whatsapp_test_mode)
    mode = "test" if test_mode else "live"

    if not settings.whatsapp_api_username or not settings.whatsapp_api_password:
        return _result(
            attempted=False,
            sent=False,
            message="WhatsApp is not configured: set WHATSAPP_API_USERNAME and WHATSAPP_API_PASSWORD.",
            mode=mode,
        )

    recipient = _normalise_mobile(settings.whatsapp_test_number if test_mode else runner_mobile)
    if not recipient:
        requirement = "WHATSAPP_TEST_NUMBER" if test_mode else "the runner's mobile number in VM_EmployeeMaster"
        return _result(
            attempted=False,
            sent=False,
            message=f"WhatsApp was not sent because {requirement} is missing or invalid.",
            mode=mode,
        )

    message = _task_message(
        task_id=task_id,
        runner=runner,
        branch=branch,
        purpose=purpose,
        task_start_datetime=task_start_datetime,
        expected_return_time=expected_return_time,
        lead_time_mins=lead_time_mins,
        test_mode=test_mode,
    )

    # Use params rather than interpolating a URL. This correctly escapes the +
    # country prefix and message text containing characters such as &, ?, or #.
    params: list[tuple[str, str]] = [
        ("username", settings.whatsapp_api_username),
        ("password", settings.whatsapp_api_password),
        ("receiverMobileNo", recipient),
        ("message", message),
    ]
    channel_id = str(settings.whatsapp_channel_id or "").strip()
    if channel_id:
        params.append(("channelId", channel_id))

    api_url = str(settings.whatsapp_api_url or "https://app.ceoitbox.com/api/v1/message/create").strip()
    try:
        timeout = httpx.Timeout(settings.request_timeout, connect=min(settings.request_timeout, 5.0))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(api_url, params=params, headers={"Accept": "application/json"})

        if response.status_code >= 400:
            if response.status_code == 401:
                error_message = "WhatsApp API authentication failed (HTTP 401). Check the company API credentials."
            elif response.status_code == 403:
                error_message = "WhatsApp API denied this request (HTTP 403). Check account and channel permissions."
            elif response.status_code == 400:
                error_message = "WhatsApp API rejected the message (HTTP 400). Check the recipient and request settings."
            else:
                error_message = f"WhatsApp API returned HTTP {response.status_code}."
            logger.warning("Runner360 WhatsApp send failed with HTTP status %s", response.status_code)
            return _result(attempted=True, sent=False, message=error_message, mode=mode)

        try:
            data = response.json()
        except ValueError:
            logger.warning("Runner360 WhatsApp API returned a non-JSON response")
            return _result(attempted=True, sent=False, message="WhatsApp API returned an unreadable response.", mode=mode)

        api_status = data.get("status") if isinstance(data, dict) else None
        api_message = str(data.get("message") or "").strip().casefold() if isinstance(data, dict) else ""
        success = api_status == 200 and api_message == "success"
        if not success:
            logger.warning("Runner360 WhatsApp API did not confirm success; status=%s", api_status)
            return _result(
                attempted=True,
                sent=False,
                message="WhatsApp API did not confirm that the message was created.",
                mode=mode,
            )

        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        provider_id = str(result.get("id") or result.get("groupId") or "")
        logger.info("Runner360 WhatsApp message created for task %s (mode=%s)", task_id, mode)
        success_message = (
            "WhatsApp test notification accepted by CEOITBOX for the configured test number."
            if test_mode
            else "WhatsApp task notification accepted by CEOITBOX."
        )
        return _result(
            attempted=True,
            sent=True,
            message=success_message,
            mode=mode,
            provider_message_id=provider_id,
        )
    except httpx.TimeoutException:
        logger.warning("Runner360 WhatsApp send timed out for task %s", task_id)
        return _result(attempted=True, sent=False, message="WhatsApp API timed out. The Runner360 task was still saved.", mode=mode)
    except httpx.HTTPError as exc:
        # Do not print exception URL details: query-based auth includes credentials.
        logger.warning("Runner360 WhatsApp request failed (%s)", type(exc).__name__)
        return _result(attempted=True, sent=False, message="Could not connect to the WhatsApp API. The Runner360 task was still saved.", mode=mode)
    except Exception as exc:  # Never let notification failures undo an assignment.
        # Log only the exception type; never risk writing a query URL or credential
        # into application logs.
        logger.error("Unexpected Runner360 WhatsApp failure (%s)", type(exc).__name__)
        return _result(attempted=True, sent=False, message="Unexpected WhatsApp error. The Runner360 task was still saved.", mode=mode)
