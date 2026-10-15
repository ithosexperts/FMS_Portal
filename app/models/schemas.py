"""
Internal, framework-agnostic data shapes used between services and
templates. These are NOT database models - the real schema lives entirely
in FMS_Config / TM_Application_Master / the application tables and is read
dynamically at runtime.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class FmsConfigRow(BaseModel):
    """One row of FMS_Config - a single stage definition for one application."""
    model_config = {"extra": "allow"}

    APP_ID: str
    FMS_NAME: str
    FMS_TYPE: str
    TABLE_NAME: str
    STAGE_NO: str
    STAGE_NAME: str
    DOER: Optional[str] = None
    DOER_EMP_CODE: Optional[str] = None
    BACKUP_DOER: Optional[str] = None
    BACKUP_EMP_CODE: Optional[str] = None
    PLANNED_COLUMN: Optional[str] = None
    ACTUAL_COLUMN: Optional[str] = None
    DISPLAY_ORDER: Optional[int] = None


class ApplicationMasterRow(BaseModel):
    """One row of TM_Application_Master - registration details for an APP_ID."""
    model_config = {"extra": "allow"}

    APP_ID: str
    APP_NAME: Optional[str] = None


class StageStatus(BaseModel):
    """Computed (never stored) status of a single stage for a single task."""
    stage_no: str
    stage_name: str
    doer: Optional[str] = None
    doer_emp_code: Optional[str] = None
    backup_doer: Optional[str] = None
    backup_emp_code: Optional[str] = None
    planned: Optional[str] = None
    actual: Optional[str] = None
    status: str  
    delay_minutes: int = 0
    is_current: bool = False
    display_order: int = 0


class TaskSummary(BaseModel):
    """One dashboard row."""
    task_id: str
    app_id: str
    application_name: str
    table_name: Optional[str] = None
    primary_key_name: Optional[str] = None
    row_id_value: Optional[str] = None
    current_stage_no: Optional[str] = None
    current_stage_name: Optional[str] = None
    assigned_doer: Optional[str] = None
    elapsed_display: str = "-"
    delay_minutes: int = 0
    # Computed in the portal for Runner360; it is not a physical DB column.
    extra_busy_minutes: Optional[int] = None
    status: str = "Pending"
    lifecycle_status: str = "Open"
    so_no: Optional[str] = None
    po_no: Optional[str] = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class FormQuestion(BaseModel):
    """One rendered field of a dynamic form (Application form or Stage form)."""
    field: str
    caption: str
    input_type: str
    value: Any = None
    editable: bool = False
    required: bool = False
    is_hidden: bool = False
    is_generic: bool = False
    rows: Optional[int] = None
    span: Optional[int] = None
    select_mode: Optional[str] = None
    select_list_values: Optional[str] = None
    select_table: Optional[str] = None
    select_column: Optional[str] = None
    dd_depends_on: Optional[str] = None
    dd_filter_column: Optional[str] = None
    show_if_column: Optional[str] = None
    show_if_operator: Optional[str] = None
    show_if_value: Optional[str] = None