"""
Permission plumbing placeholder.

Nothing in this project hardcodes a role check - every gate goes through
`can(action, context)` so a future auth/role system can be plugged in by
replacing ONLY this file (e.g. reading roles from get_current_user()).
"""
from __future__ import annotations

from typing import Any

from app.auth.request_context import get_current_identity

# Actions the UI currently gates. Extend freely - callers pass a string key.
ACTION_EDIT_CURRENT_STAGE = "edit_current_stage"
ACTION_VIEW_DEVELOPER_HUB = "view_developer_hub"
ACTION_EDIT_PAST_STAGE = "edit_past_stage"


def can(action: str, context: dict[str, Any] | None = None) -> bool:
    """
    Central permission check.

    Today every action is allowed (no auth system wired in yet). Swap the
    body of this function once roles/users are available - every caller
    (dashboard, stage form, developer hub) already routes through here.
    """
    context = context or {}

    if action == ACTION_EDIT_PAST_STAGE:
        # Past stages are never editable, regardless of role.
        return False

    identity = get_current_identity()
    if identity is None:
        # Auth is not configured for this environment.
        return True

    if str(identity.role_id).strip() == "1":
        return True

    permission_keys = {str(p).strip().lower() for p in identity.permissions}
    normalized_action = str(action or "").strip().lower()

    candidates = {
        normalized_action,
        normalized_action.replace("_", ""),
        f"can_{normalized_action}",
        f"can{normalized_action.replace('_', '')}",
    }
    return any(candidate in permission_keys for candidate in candidates)
