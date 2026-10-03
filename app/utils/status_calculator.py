"""
Status calculation for both a single stage and an overall task, per the
rules:

    Completed   - Actual exists
    In Progress - Planned exists, Actual is null, now <= Planned
    Delayed     - Planned exists, Actual is null, now >  Planned
    Pending     - future stage (no Planned yet)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils.delay_calculator import calculate_delay_minutes, is_blank, parse_datetime
from app.utils.stage_calculator import get_current_stage, is_stage_visible, is_task_completed, sorted_stages

STATUS_COMPLETED = "Completed"
STATUS_IN_PROGRESS = "In Progress"
STATUS_DELAYED = "Delayed"
STATUS_PENDING = "Pending"
STATUS_EMPTY = ""

# Colour codes used consistently by dashboard badges + timeline cards.
STATUS_COLORS = {
    STATUS_COMPLETED: "green",
    STATUS_IN_PROGRESS: "blue",
    STATUS_DELAYED: "red",
    STATUS_PENDING: "grey",
}


def _row_value(row: dict[str, Any], field_name: Any) -> Any:
    """Case-insensitive dictionary lookup."""
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


def _resolve_stage_column_name(stage: dict[str, Any], row: dict[str, Any], column_kind: str) -> str | None:
    configured_name = stage.get("PLANNED_COLUMN") if column_kind == "planned" else stage.get("ACTUAL_COLUMN")
    if configured_name:
        value = _row_value(row, configured_name)
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

    fallback = _find_best_key_by_suffix(row, stage_prefix, suffixes)
    if fallback:
        return fallback

    return None


def _stage_planned_actual_values(stage: dict[str, Any], row: dict[str, Any]) -> tuple[Any, Any]:
    planned_col = _resolve_stage_column_name(stage, row, "planned")
    actual_col = _resolve_stage_column_name(stage, row, "actual")
    return _row_value(row, planned_col), _row_value(row, actual_col)


def calculate_stage_status(planned: Any, actual: Any, doer: Any = None, now: datetime | None = None) -> str:
    """Status of a single stage."""
    if not is_blank(actual):
        return STATUS_COMPLETED
        
    # ⚡ FIX: Removed Doer check as requested. 
    # Stage is Pending ONLY if Planned is blank.
    if is_blank(planned):
        return STATUS_PENDING

    now = now or datetime.now()
    planned_dt = parse_datetime(planned)
    if planned_dt is not None and now > planned_dt:
        return STATUS_DELAYED
        
    return STATUS_IN_PROGRESS


def build_timeline(stage_configs: list[dict[str, Any]], task_row: dict[str, Any]) -> list[dict[str, Any]]:
    """Full ordered stage-by-stage timeline for the Task Details page."""
    now = datetime.now()
    current = get_current_stage(stage_configs, task_row)
    current_no = current.get("STAGE_NO") if current else None

    timeline = []
    any_planned = False
    for stage in sorted_stages(stage_configs):
        if not is_stage_visible(stage, task_row):
            timeline.append({
                "stage_no": stage.get("STAGE_NO"),
                "stage_name": stage.get("STAGE_NAME"),
                "doer": None,
                "doer_emp_code": stage.get("DOER_EMP_CODE"),
                "backup_doer": stage.get("BACKUP_DOER"),
                "backup_emp_code": stage.get("BACKUP_EMP_CODE"),
                "planned": None,
                "actual": None,
                "planned_column": stage.get("PLANNED_COLUMN"),
                "actual_column": stage.get("ACTUAL_COLUMN"),
                "status": STATUS_PENDING,
                "color": STATUS_COLORS[STATUS_PENDING],
                "delay_minutes": 0,
                "is_current": False,
                "visible": False,
            })
            continue
        planned, actual = _stage_planned_actual_values(stage, task_row)
        if not is_blank(planned):
            any_planned = True
        stage_no = str(stage.get("STAGE_NO") or "")
        stage_prefix = _normalize(stage_no)
        doer_key = _find_best_key_by_suffix(task_row, stage_prefix, ("doer",))
        doer_code_key = _find_best_key_by_suffix(task_row, stage_prefix, ("doerempcode",))
        doer = (
            stage.get("DOER")
            or stage.get("DOER_EMP_CODE")
            or _row_value(task_row, doer_key)
            or _row_value(task_row, doer_code_key)
        )
        
        status = calculate_stage_status(planned, actual, doer, now)

        # Pack to Dispatch S3 completion is also explicitly represented by
        # S3_TaskStatus / S3_ALTimeStamp. Keep the dashboard stage status in
        # sync with the custom S3 form even when FMS_Config ACTUAL_COLUMN is
        # aliased or differs from the physical S3 result column.
        if _normalize(stage_no) == "s3":
            s3_status_raw = _row_value(task_row, "S3_TaskStatus")
            s3_status_norm = _normalize(s3_status_raw)
            s3_actual_value = None
            for candidate in (
                "S3_ALTimeStamp",
                "S3_ActualTimestamp",
                "S3_ActualDateTime",
                "S3_ALTime",
            ):
                value = _row_value(task_row, candidate)
                if value not in (None, "") and str(value).strip().casefold() not in {"null", "none"}:
                    s3_actual_value = value
                    break
            if s3_status_norm in {
                "done", "donedelay", "completed", "complete", "closed", "closedelay"
            } or s3_actual_value is not None:
                status = STATUS_COMPLETED
                actual = s3_actual_value or actual

        timeline.append({
            "stage_no": stage.get("STAGE_NO"),
            "stage_name": stage.get("STAGE_NAME"),
            "doer": doer,
            "doer_emp_code": stage.get("DOER_EMP_CODE"),
            "backup_doer": stage.get("BACKUP_DOER"),
            "backup_emp_code": stage.get("BACKUP_EMP_CODE"),
            "planned": planned,
            "actual": actual,
            "planned_column": stage.get("PLANNED_COLUMN"),
            "actual_column": stage.get("ACTUAL_COLUMN"),
            "status": status,
            "color": STATUS_COLORS[status],
            "delay_minutes": calculate_delay_minutes(planned, actual, now),
            "is_current": stage.get("STAGE_NO") == current_no,
            "visible": True,
        })
    if not any_planned:
        return timeline
    return timeline


def calculate_task_status(stage_configs: list[dict[str, Any]], task_row: dict[str, Any]) -> str:
    """
    Overall task status (shown on the dashboard row).
    Defaults to Pending until at least one stage is In Progress or Delayed.
    """
    stages = sorted_stages(stage_configs)
    if not stages:
        return STATUS_EMPTY

    has_any_planned = False
    for stage in stages:
        if not is_stage_visible(stage, task_row):
            continue
        planned, _actual = _stage_planned_actual_values(stage, task_row)
        if not is_blank(planned):
            has_any_planned = True
            break

    if not has_any_planned:
        return STATUS_EMPTY

    if is_task_completed(stage_configs, task_row):
        return STATUS_COMPLETED

    current = get_current_stage(stage_configs, task_row)
    if not current:
        return STATUS_PENDING

    planned_col = _resolve_stage_column_name(current, task_row, "planned")
    planned_value = _row_value(task_row, planned_col)
    planned_dt = parse_datetime(planned_value)

    if planned_dt and datetime.now() > planned_dt:
        return STATUS_DELAYED
        
    return STATUS_IN_PROGRESS