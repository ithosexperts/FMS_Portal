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
from app.services.hierarchy_service import is_user_in_hierarchy_sync

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

    # In every case the user must have VM_EmployeeMaster.IsEmployed = 'Y'.
    # Any user where IsEmployed is 'N' (or not 'Y') is denied all access.
    raw_employee = getattr(identity, "raw_employee", {}) or {}
    is_employed = None
    if isinstance(raw_employee, dict):
        for k in ("IsEmployed", "is_employed", "ISEMPLOYED", "Active", "active", "ActiveStatus", "activestatus"):
            if k in raw_employee and raw_employee[k] is not None:
                is_employed = str(raw_employee[k]).strip().upper()
                break
    if is_employed is not None and is_employed not in ("Y", "1", "TRUE"):
        return False

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

    # Resolve application TYPE ('F', 'A', etc.)
    app_type = str(
        context.get("app_type")
        or (context.get("fms") or {}).get("type")
        or context.get("Type")
        or ""
    ).strip().upper()

    # Resolve application LOGIN_TYPE from context
    raw_lt = (
        context.get("login_type")
        or (context.get("fms") or {}).get("login_type")
        or context.get("LOGIN_TYPE")
        or (context.get("fms") or {}).get("LOGIN_TYPE")
        or ""
    )
    is_sso = str(raw_lt).strip().upper() == "SSO"
    is_admin = user_role_id in ("1", "2", "3")

    # =========================================================================
    # TYPE F APPLICATIONS (TM_Application_Master.Type = 'F')
    # Permissions are decided PER TASK, never per stage, and NEVER from FMS_Config.
    # Can_View and Can_Edit in FMS_Application_Rights are IGNORED.
    # View/Edit come only from Rules A-D:
    #   A. Assigned doer: user EC == DoerCode in Z_KPI_MasterRecord
    #   B. Manager chain: user EC appears in ManagerChain of DoerCode
    #   C. Admin role: RoleID IN (1, 2, 3) (with IsEmployed = 'Y')
    #   D. ALL: task's DoerCode is 'ALL' (any case)
    # A task with no row in Z_KPI_MasterRecord (stage not yet planned) is accessible
    # ONLY under Rule C (Admin).
    # =========================================================================
    if app_type == "F":
        if is_view_action or is_edit_action:
            # Rule C: Admin role (RoleID IN (1, 2, 3) and IsEmployed = 'Y')
            if is_admin:
                return True

            # If task has no row in Z_KPI_MasterRecord (stage not yet planned), only Rule C has access.
            has_kpi_row = context.get("has_kpi_row", True)
            if not has_kpi_row:
                return False

            doer_code = str(context.get("kpi_doer_code") or context.get("doer_code") or "").strip()
            if not doer_code:
                # No doer code assigned on KPI row -> only Admin (Rule C) has access
                return False

            # Rule D: Task's DoerCode is 'ALL' (any case) -> open to every active employee
            if doer_code.upper() == "ALL":
                return True

            # Rule A: Assigned doer (match on employee codes only, never on names)
            if user_ec and user_ec == doer_code:
                return True

            # Rule B: Manager chain (user EC appears in VM_EmployeeMaster.ManagerChain of DoerCode)
            if user_ec and is_user_in_hierarchy_sync(user_ec, doer_code):
                return True

            # In Type F, Can_View and Can_Edit in FMS_Application_Rights are IGNORED
            return False

        # Other rights on Type F (Can_Add, Can_Delete, Can_Export, Can_Approve,
        # Can_Reassign, Can_Close, Can_Reopen) still come from FMS_Application_Rights / permissions.
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

    # =========================================================================
    # TYPE A & NOR APPLICATIONS
    # =========================================================================
    # 1. Role ID 1, 2, and 3:
    # If Application Login type is SSO -> Can View and Can Edit by default.
    # If NOR -> only Can View by default.
    if is_admin:
        if is_view_action:
            return True
        if is_edit_action:
            if is_sso:
                return True
        elif not is_edit_action:
            return True

    # 2. Check FMS_Application_Rights / NOR check for Can View
    if is_view_action and (context.get("can_view_rights") or context.get("login_type") == "NOR"):
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
