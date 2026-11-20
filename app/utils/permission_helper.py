"""
Permission plumbing helper.

Implements role and doer permission checks:
1. If the Application Login type is SSO, Role ID 1, 2, and 3 have Can View and Can Edit rights by default.
   If NOR, Role ID 1, 2, and 3 have only Can View rights by default.
2. Doer EC and Backup EC doer have Can View and Can Edit rights by default to that app and its stage forms.
3. Temp doer EC has rights only within the I_FromDateTime to I_ToDateTime window.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.auth.request_context import get_current_identity

# Actions the UI currently gates. Extend freely - callers pass a string key.
ACTION_EDIT_CURRENT_STAGE = "edit_current_stage"
ACTION_VIEW_DEVELOPER_HUB = "view_developer_hub"
ACTION_EDIT_PAST_STAGE = "edit_past_stage"
ACTION_CAN_VIEW = "can_view"
ACTION_CAN_EDIT = "can_edit"


def _is_within_temp_doer_window(from_val: Any, to_val: Any) -> bool:
    """Check if current datetime is within the specified From/To window."""
    if not from_val or not to_val:
        return False
    from_str = str(from_val).strip()
    to_str = str(to_val).strip()
    if not from_str or not to_str or from_str.upper() in ("NULL", "NONE", "-") or to_str.upper() in ("NULL", "NONE", "-"):
        return False
    now = datetime.now()
    from_dt = None
    to_dt = None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ):
        if not from_dt:
            try:
                from_dt = datetime.strptime(from_str, fmt)
            except ValueError:
                pass
        if not to_dt:
            try:
                to_dt = datetime.strptime(to_str, fmt)
            except ValueError:
                pass
    if from_dt and to_dt:
        return from_dt <= now <= to_dt
    return False


def can(action: str, context: dict[str, Any] | None = None) -> bool:
    """
    Central permission check.
    """
    context = context or {}

    if action == ACTION_EDIT_PAST_STAGE:
        # Past stages are never editable, regardless of role.
        return False

    identity = get_current_identity()
    if identity is None:
        # Auth is not configured for this environment.
        return True

    user_role_id = str(getattr(identity, "role_id", "") or "").strip()
    user_ec = str(getattr(identity, "employee_code", "") or "").strip()

    normalized_action = str(action or "").strip().lower()
    is_edit_action = (
        normalized_action in (
            ACTION_EDIT_CURRENT_STAGE,
            "edit_current_stage",
            "edit_stage",
            "can_edit",
            "edit",
        )
        or normalized_action.startswith("edit_")
        or normalized_action.startswith("can_edit")
    )
    is_view_action = (
        normalized_action in (
            ACTION_CAN_VIEW,
            "can_view",
            "view",
            "view_stage",
            "view_app",
            "view_dashboard",
        )
        or normalized_action.startswith("view_")
        or normalized_action.startswith("can_view")
    )

    # Resolve application LOGIN_TYPE from context
    raw_lt = (
        context.get("login_type")
        or (context.get("fms") or {}).get("login_type")
        or context.get("LOGIN_TYPE")
        or (context.get("fms") or {}).get("LOGIN_TYPE")
        or ""
    )
    is_sso = str(raw_lt).strip().upper() == "SSO"

    # 1. Role ID 1, 2, and 3:
    # If Application Login type is SSO -> Can View and Can Edit by default.
    # If NOR -> only Can View by default.
    if user_role_id in ("1", "2", "3"):
        if is_view_action:
            return True
        if is_edit_action:
            if is_sso:
                return True
            # If NOR, check if user is the assigned doer below before denying edit.
        elif not is_edit_action:
            return True

    # 2. Check Doer EC, Backup EC doer, Temp doer EC:
    # "we will Give Doer EC , Back up EC doer Can view and Can edit rights by deafult of that app and its stage forms
    #  and Temp doer EC will have rights to it only for I_FromDateTime to I_ToDateTime date&Time the Permission should be there"
    if user_ec:
        stages_to_check = []
        if context.get("stage") and isinstance(context["stage"], dict):
            stages_to_check.append(context["stage"])
        if context.get("current_stage") and isinstance(context["current_stage"], dict):
            stages_to_check.append(context["current_stage"])
        if context.get("stage_configs") and isinstance(context["stage_configs"], list):
            stages_to_check.extend([s for s in context["stage_configs"] if isinstance(s, dict)])
        if context.get("fms") and isinstance(context["fms"], dict):
            stages_to_check.append(context["fms"])
            if isinstance(context["fms"].get("stages"), list):
                stages_to_check.extend([s for s in context["fms"]["stages"] if isinstance(s, dict)])
            if isinstance(context["fms"].get("stage_configs"), list):
                stages_to_check.extend([s for s in context["fms"]["stage_configs"] if isinstance(s, dict)])

        for stg in stages_to_check:
            d_ec = str(
                stg.get("DOER_EMP_CODE")
                or stg.get("Doer_EC")
                or stg.get("doer_ec")
                or stg.get("DOER_EC")
                or stg.get("DoerEC")
                or ""
            ).strip()
            b_ec = str(
                stg.get("BACKUP_EMP_CODE")
                or stg.get("BackupDoer_EC")
                or stg.get("Backup_Doer_EC")
                or stg.get("backup_doer_ec")
                or stg.get("BackupDoerEC")
                or stg.get("backup_ec")
                or ""
            ).strip()
            t_ec = str(
                stg.get("TEMP_DOER_EMP_CODE")
                or stg.get("TempDoer_EC")
                or stg.get("Temp_Doer_EC")
                or stg.get("temp_doer_ec")
                or stg.get("TempDoerEC")
                or ""
            ).strip()

            # Doer EC and Backup EC doer have Can View and Can Edit by default
            if user_ec in (d_ec, b_ec) or d_ec.upper() == "ALL" or b_ec.upper() == "ALL":
                return True

            # Temp Doer EC has rights only for I_FromDateTime to I_ToDateTime
            if user_ec == t_ec:
                from_dt = (
                    stg.get("I_FROM_DATE_TIME")
                    or stg.get("I_FromDateTime")
                    or stg.get("from_date_time")
                    or stg.get("fromdatetime")
                )
                to_dt = (
                    stg.get("I_TO_DATE_TIME")
                    or stg.get("I_ToDateTime")
                    or stg.get("to_date_time")
                    or stg.get("todatetime")
                )
                if _is_within_temp_doer_window(from_dt, to_dt):
                    return True

    # 3. Explicit permissions configured in RBAC / TM_User_Access_Master
    permission_keys = {str(p).strip().lower() for p in getattr(identity, "permissions", []) or []}
    candidates = {
        normalized_action,
        normalized_action.replace("_", ""),
        f"can_{normalized_action}",
        f"can{normalized_action.replace('_', '')}",
    }
    if any(candidate in permission_keys for candidate in candidates):
        return True

    return False
