from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest

from app.auth.request_context import (
    reset_current_identity,
    set_current_identity,
)
from app.utils.permission_helper import (
    ACTION_CAN_EDIT,
    ACTION_CAN_VIEW,
    ACTION_EDIT_CURRENT_STAGE,
    ACTION_EDIT_PAST_STAGE,
    can,
    _is_within_temp_doer_window,
)


def _mock_identity(role_id: str | int, employee_code: str, permissions: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=1,
        role_id=str(role_id),
        employee_code=employee_code,
        username=employee_code,
        permissions=permissions or [],
    )


@pytest.fixture(autouse=True)
def cleanup_identity():
    yield
    reset_current_identity()


def test_role_1_2_3_sso_vs_nor():
    for role in ("1", "2", "3", 1, 2, 3):
        ident = _mock_identity(role, "ADMIN_EMP")
        set_current_identity(ident)

        # SSO: Can View AND Can Edit by default
        assert can(ACTION_CAN_VIEW, {"login_type": "SSO"}) is True
        assert can(ACTION_CAN_EDIT, {"login_type": "SSO"}) is True
        assert can(ACTION_EDIT_CURRENT_STAGE, {"login_type": "SSO"}) is True

        # NOR: Can View only by default; Can Edit is False (unless assigned doer)
        assert can(ACTION_CAN_VIEW, {"login_type": "NOR"}) is True
        assert can(ACTION_CAN_EDIT, {"login_type": "NOR"}) is False
        assert can(ACTION_EDIT_CURRENT_STAGE, {"login_type": "NOR"}) is False

        # Past stages are never editable even in SSO
        assert can(ACTION_EDIT_PAST_STAGE, {"login_type": "SSO"}) is False


def test_doer_ec_and_backup_ec_rights():
    user_ec = "EMP_DOER_01"
    backup_ec = "EMP_BACKUP_02"
    random_user_ec = "EMP_OTHER_99"

    # Stage context
    stage_data = {
        "STAGE_NO": "S1",
        "STAGE_NAME": "Review",
        "DOER_EMP_CODE": user_ec,
        "BACKUP_EMP_CODE": backup_ec,
    }

    # 1. Doer EC has Can View and Can Edit
    set_current_identity(_mock_identity(4, user_ec))
    assert can(ACTION_CAN_VIEW, {"stage": stage_data}) is True
    assert can(ACTION_EDIT_CURRENT_STAGE, {"stage": stage_data}) is True
    assert can(ACTION_CAN_EDIT, {"stage": stage_data}) is True

    # 2. Backup EC doer has Can View and Can Edit
    set_current_identity(_mock_identity(4, backup_ec))
    assert can(ACTION_CAN_VIEW, {"stage": stage_data}) is True
    assert can(ACTION_EDIT_CURRENT_STAGE, {"stage": stage_data}) is True
    assert can(ACTION_CAN_EDIT, {"stage": stage_data}) is True

    # 3. Other employee has neither
    set_current_identity(_mock_identity(4, random_user_ec))
    assert can(ACTION_CAN_VIEW, {"stage": stage_data}) is False
    assert can(ACTION_EDIT_CURRENT_STAGE, {"stage": stage_data}) is False


def test_temp_doer_ec_time_window():
    temp_ec = "EMP_TEMP_03"
    set_current_identity(_mock_identity(4, temp_ec))

    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    last_week = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    next_week = (now + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    # Case A: Active window (yesterday -> tomorrow) => Granted
    stage_active = {
        "STAGE_NO": "S1",
        "DOER_EMP_CODE": "EMP_MAIN",
        "TEMP_DOER_EMP_CODE": temp_ec,
        "I_FROM_DATE_TIME": yesterday,
        "I_TO_DATE_TIME": tomorrow,
    }
    assert can(ACTION_CAN_VIEW, {"stage": stage_active}) is True
    assert can(ACTION_EDIT_CURRENT_STAGE, {"stage": stage_active}) is True

    # Case B: Expired window (last week -> yesterday) => Denied
    stage_expired = {
        "STAGE_NO": "S1",
        "DOER_EMP_CODE": "EMP_MAIN",
        "TEMP_DOER_EMP_CODE": temp_ec,
        "I_FROM_DATE_TIME": last_week,
        "I_TO_DATE_TIME": yesterday,
    }
    assert can(ACTION_CAN_VIEW, {"stage": stage_expired}) is False
    assert can(ACTION_EDIT_CURRENT_STAGE, {"stage": stage_expired}) is False

    # Case C: Future window (tomorrow -> next week) => Denied
    stage_future = {
        "STAGE_NO": "S1",
        "DOER_EMP_CODE": "EMP_MAIN",
        "TEMP_DOER_EMP_CODE": temp_ec,
        "I_FROM_DATE_TIME": tomorrow,
        "I_TO_DATE_TIME": next_week,
    }
    assert can(ACTION_CAN_VIEW, {"stage": stage_future}) is False
    assert can(ACTION_EDIT_CURRENT_STAGE, {"stage": stage_future}) is False


def test_temp_doer_window_helper():
    now = datetime.now()
    from_valid = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    to_valid = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    assert _is_within_temp_doer_window(from_valid, to_valid) is True
    assert _is_within_temp_doer_window(None, to_valid) is False
    assert _is_within_temp_doer_window(from_valid, "") is False
    assert _is_within_temp_doer_window("NULL", "NULL") is False
