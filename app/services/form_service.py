"""
Dynamic form assembly - turns raw metadata + a data row into a list of
FormQuestion objects. Used independently by the Application Form (always
read-only) and the Stage Form (only the current stage is editable).
"""
from __future__ import annotations

from typing import Any

from app.api import metadata_api
from app.models.schemas import FormQuestion
from app.utils.caption_formatter import format_caption


def _resolve_text_rows(col: dict[str, Any], default_rows: int = 2) -> int:
    raw_rows = col.get("text_rows", default_rows)
    try:
        return max(1, int(raw_rows))
    except (TypeError, ValueError):
        return default_rows


def _row_value(row: dict[str, Any], field_name: str) -> Any:
    if field_name in row:
        return row[field_name]

    lowered = field_name.lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    for key, value in row.items():
        key_lower = str(key).lower()
        key_compact = "".join(ch for ch in key_lower if ch.isalnum())
        if key_lower == lowered or key_compact == compact:
            return value
    return None


def _resolve_input_type(col: dict[str, Any]) -> str:
    value = str(col.get("input_type") or "").strip().lower()

    mapping = {
        "text": "text",
        "textbox": "text",
        "number": "number",
        "numeric": "number",
        "textarea": "textarea",
        "multiline": "textarea",
        "file": "file",
        "image": "file",
        "datetime": "datetime-local",
        "datetime-local": "datetime-local",
        "date": "date",
        "checkbox": "checkbox",
        "check": "checkbox",
        "select": "select",
        "dropdown": "select",
    }

    return mapping.get(value, "text")


def _column_by_name(columns: list[dict[str, Any]], field_name: str) -> dict[str, Any] | None:
    lowered = field_name.lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    for col in columns:
        name = str(col.get("name") or "")
        name_lower = name.lower()
        name_compact = "".join(ch for ch in name_lower if ch.isalnum())
        if name_lower == lowered or name_compact == compact:
            return col
    return None


def _field_span(input_type: str, value: Any) -> int | None:
    text = "" if value is None else str(value).strip()
    if input_type != "textarea":
        return None
    if len(text) >= 220: return 3
    if len(text) >= 100: return 2
    return 2


def _starts_with(name: str, prefix: str) -> bool:
    return str(name).strip().lower().startswith(prefix.strip().lower())


def _collect_prefixed_names(
    columns: list[dict[str, Any]],
    row: dict[str, Any],
    prefix: str,
) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    for col in columns:
        col_name = str(col.get("name") or "").strip()
        if not col_name or not _starts_with(col_name, prefix):
            continue
        lowered = col_name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        names.append(col_name)

    for key in row.keys():
        key_name = str(key or "").strip()
        if not key_name or not _starts_with(key_name, prefix):
            continue
        lowered = key_name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        names.append(key_name)

    return names


def _build_synthetic_question(field: str, caption: str, value: Any) -> FormQuestion:
    col = {
        "name": field,
        "publish_name": caption,
        "input_type": "text",
        "nullable": True,
        "is_readonly": True,
        "is_editable": False,
    }
    return _build_question(col, value, editable=False, force_readonly=True)


def _is_truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _build_question(
    col: dict[str, Any],
    value: Any,
    editable: bool,
    *,
    force_readonly: bool = False,
    is_generic: bool = False,
) -> FormQuestion:
    input_type = _resolve_input_type(col)
    rows = _resolve_text_rows(col) if input_type == "textarea" else None

    publish_name = col.get("publish_name")
    caption = str(publish_name).strip() if publish_name else format_caption(str(col.get("name") or ""))

    is_mandatory = col.get("is_mandatory")
    required = True if _is_truthy(is_mandatory) else not col.get("nullable", True)

    is_hidden = _is_truthy(col.get("is_hidden", False))

    final_editable = editable and not force_readonly
    if final_editable:
        is_readonly = _is_truthy(col.get("is_readonly", False))
        is_editable_col = not (str(col.get("is_editable", "1")).strip().lower() in ("0", "false", "no", "n"))
        final_editable = not is_readonly and is_editable_col

    return FormQuestion(
        field=str(col.get("name") or ""),
        caption=caption,
        input_type=input_type,
        value=value,
        editable=final_editable,
        required=required,
        is_hidden=is_hidden,
        is_generic=is_generic,
        rows=rows,
        span=_field_span(input_type, value),
        select_mode=col.get("select_mode"),
        select_list_values=col.get("select_list_values"),
        select_table=col.get("select_table"),
        select_column=col.get("select_column"),
        dd_depends_on=col.get("dd_depends_on"),
        dd_filter_column=col.get("dd_filter_column"),
        show_if_column=col.get("show_if_column"),
        show_if_operator=col.get("show_if_operator"),
        show_if_value=col.get("show_if_value"),
    )


async def build_application_form(table_name: str, row: dict[str, Any]) -> list[FormQuestion]:
    columns = await metadata_api.get_metadata(table_name)
    questions = []
    
    for col in columns:
        if not col.get("name"): continue
        value = _row_value(row, col["name"])
        questions.append(_build_question(col, value, editable=False, force_readonly=True))

    return questions


async def build_dashboard_modal_sections(
    table_name: str,
    row: dict[str, Any],
    task_id: str,
    primary_key: str,
) -> list[dict[str, Any]]:
    columns = await metadata_api.get_metadata(table_name)

    row_identifier = (
        _row_value(row, primary_key)
        or _row_value(row, "TaskID")
        or _row_value(row, "Task_ID")
        or _row_value(row, "RowID")
        or task_id
    )

    identity_fields: list[FormQuestion] = [
        _build_synthetic_question("__row_task_id", "ROW ID / Task ID", row_identifier),
        _build_synthetic_question(
            "__primary_key",
            f"Primary Key ({primary_key})",
            _row_value(row, primary_key) or row_identifier,
        ),
    ]

    for name in _collect_prefixed_names(columns, row, "pk"):
        col = _column_by_name(columns, name) or {"name": name, "input_type": "text", "nullable": True}
        identity_fields.append(
            _build_question(col, _row_value(row, name), editable=False, force_readonly=True)
        )

    def prefixed_fields(prefix: str) -> list[FormQuestion]:
        grouped: list[FormQuestion] = []
        for name in _collect_prefixed_names(columns, row, prefix):
            col = _column_by_name(columns, name) or {"name": name, "input_type": "text", "nullable": True}
            grouped.append(_build_question(col, _row_value(row, name), editable=False, force_readonly=True))
        return grouped

    sections = [
        {"title": "Identity", "fields": identity_fields},
        {"title": "Input Details", "fields": prefixed_fields("i_")},
    ]
    return sections


def build_stage_form(
    stage: dict[str, Any],
    row: dict[str, Any],
    editable: bool,
    table_columns: list[dict[str, Any]] | None = None,
) -> list[FormQuestion]:
    stage_no = str(stage.get("STAGE_NO") or "")
    planned_col = str(stage.get("PLANNED_COLUMN") or "")
    actual_col = str(stage.get("ACTUAL_COLUMN") or "")
    prefix = f"{stage_no}_".lower() if stage_no else ""

    columns = table_columns or []
    columns_by_key = {
        str(col.get("name") or "").lower(): col
        for col in columns
        if col.get("name")
    }

    ordered_stage_cols: list[dict[str, Any]] = []
    for col in columns:
        name = str(col.get("name") or "")
        if not name:
            continue
        lowered = name.lower()
        if lowered == planned_col.lower() or lowered == actual_col.lower() or (prefix and lowered.startswith(prefix)):
            ordered_stage_cols.append(col)

    for special_name in (planned_col, actual_col):
        if special_name and special_name.lower() not in columns_by_key:
            ordered_stage_cols.append({"name": special_name, "input_type": "text", "nullable": True})

    fields: list[FormQuestion] = []
    seen: set[str] = set()

    for col in ordered_stage_cols:
        name = str(col.get("name") or "")
        if not name: continue
        lowered = name.lower()
        if lowered in seen: continue
        seen.add(lowered)

        value = _row_value(row, name)

        # Let saved Metadata Explorer settings decide editability.
        # Only the planned column stays read-only by default.
        force_readonly = lowered == planned_col.lower() if planned_col else False

        fields.append(_build_question(col, value, editable=editable, force_readonly=force_readonly))

    return fields