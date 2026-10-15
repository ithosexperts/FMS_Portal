"""
Current-stage calculation.

The current stage is NEVER stored in the database - it is derived, every
time, from the Planned/Actual columns declared per stage in FMS_Config:

    first stage where PLANNED is not null AND ACTUAL is null
"""
from __future__ import annotations

from typing import Any

from app.utils.delay_calculator import is_blank


def _row_value(row: dict[str, Any], field_name: Any) -> Any:
    """Case-insensitive dictionary lookup to fix API casing mismatches."""
    if not field_name:
        return None
    name_str = str(field_name)
    if name_str in row:
        return row[name_str]
    lowered = name_str.lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    for key, value in row.items():
        key_lower = str(key).lower()
        key_compact = "".join(ch for ch in key_lower if ch.isalnum())
        if key_lower == lowered or key_compact == compact:
            return value
    return None


def _normalize(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _find_best_key_by_suffix(
    row: dict[str, Any],
    stage_prefix: str,
    suffixes: tuple[str, ...],
) -> str | None:
    staged_match = None
    staged_match_with_value = None
    global_match = None
    global_match_with_value = None

    for key in row.keys():
        normalized_key = _normalize(key)
        if not any(normalized_key.endswith(suffix) for suffix in suffixes):
            continue
        key_name = str(key)
        has_value = not is_blank(_row_value(row, key_name))
        if stage_prefix and stage_prefix in normalized_key:
            if staged_match is None:
                staged_match = key_name
            if has_value and staged_match_with_value is None:
                staged_match_with_value = key_name
        else:
            if global_match is None:
                global_match = key_name
            if has_value and global_match_with_value is None:
                global_match_with_value = key_name

    return staged_match_with_value or staged_match or global_match_with_value or global_match


def _resolve_stage_column_name(stage: dict[str, Any], task_row: dict[str, Any], column_kind: str) -> str | None:
    """Resolve configured planned/actual column, with stage-based fallback discovery."""
    configured_name = stage.get("PLANNED_COLUMN") if column_kind == "planned" else stage.get("ACTUAL_COLUMN")
    if configured_name:
        value = _row_value(task_row, configured_name)
        if value is not None:
            return str(configured_name)

    stage_no = str(stage.get("STAGE_NO") or "").strip()
    if not stage_no:
        return str(configured_name) if configured_name else None

    stage_prefix = _normalize(stage_no)
    if not stage_prefix:
        return str(configured_name) if configured_name else None

    planned_suffixes = ("tspltimestamp", "pltimestamp", "plannedtimestamp", "planneddatetime", "pltime")
    actual_suffixes = ("tsaltimestamp", "altimestamp", "actualtimestamp", "actualdatetime", "altime")
    suffixes = planned_suffixes if column_kind == "planned" else actual_suffixes

    fallback = _find_best_key_by_suffix(task_row, stage_prefix, suffixes)
    if fallback:
        return fallback

    return None


def _stage_planned_actual_values(stage: dict[str, Any], task_row: dict[str, Any]) -> tuple[Any, Any]:
    planned_col = _resolve_stage_column_name(stage, task_row, "planned")
    actual_col = _resolve_stage_column_name(stage, task_row, "actual")
    return _row_value(task_row, planned_col), _row_value(task_row, actual_col)




def _has_real_value(value: Any) -> bool:
    """Treat API null-like strings as empty as well as Python None/blank."""
    if value is None:
        return False
    text = str(value).strip().lower()
    return text not in {"", "null", "none", "nan"}


def is_stage1_visible(task_row: dict[str, Any]) -> bool:
    """
    Decide whether Stage 1 is visible.

    PickNPack uses S1_PickID as an additional Stage-1 visibility gate.
    Other FMS processes, such as Runner360, do not have S1_PickID and
    therefore must not be hidden merely because that column does not exist.

    The gate controls stage visibility only; it must never remove a valid
    task from the dashboard.
    """
    planned = _row_value(task_row, "S1_PLTimeStamp")

    # If the row has no PickID field at all, this FMS does not use the
    # PickNPack visibility rule. Runner360 falls into this path.
    has_pick_id_field = any(
        str(key).strip().lower().replace("_", "") in {"s1pickid", "pickid"}
        for key in task_row.keys()
    )
    if not has_pick_id_field:
        return _has_real_value(planned)

    pick_id = _row_value(task_row, "S1_PickID") or _row_value(task_row, "PickID")
    return _has_real_value(planned) and _has_real_value(pick_id)


def is_stage_visible(stage: dict[str, Any], task_row: dict[str, Any]) -> bool:
    """Apply the S1 gate before exposing S1 or any later stage."""
    stage_no = _normalize(stage.get("STAGE_NO"))
    if stage_no == "s1":
        return is_stage1_visible(task_row)
    if stage_no:
        # All stages after S1 are exposed only when S1 itself is visible.
        return is_stage1_visible(task_row)
    return True

def sorted_stages(stage_configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stages for one FMS, ordered by DISPLAY_ORDER (falls back to STAGE_NO)."""
    def sort_key(stage: dict[str, Any]):
        order = stage.get("DISPLAY_ORDER")
        if order is not None:
            try:
                return (0, int(order))
            except (TypeError, ValueError):
                pass
        return (1, str(stage.get("STAGE_NO", "")))

    return sorted(stage_configs, key=sort_key)


def get_current_stage(stage_configs: list[dict[str, Any]], task_row: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return the FMS_Config row of the current stage, or None when the task
    has not started yet (no stage planned) or is fully completed
    (every stage's Actual is filled).
    """
    stages = sorted_stages(stage_configs)
    for stage in stages:
        if not is_stage_visible(stage, task_row):
            continue
        planned, actual = _stage_planned_actual_values(stage, task_row)

        # Planned is NOT null AND Actual IS null => current/editable stage.
        if not is_blank(planned) and is_blank(actual):
            return stage
            
    return None


def is_task_completed(stage_configs: list[dict[str, Any]], task_row: dict[str, Any]) -> bool:
    """True when every stage that has a Planned value also has an Actual value."""
    stages = sorted_stages(stage_configs)
    if not stages:
        return False
    any_planned = False
    for stage in stages:
        if not is_stage_visible(stage, task_row):
            continue
        planned, actual = _stage_planned_actual_values(stage, task_row)
        if not is_blank(planned):
            any_planned = True
            if is_blank(actual):
                return False
    return any_planned