"""Shared employee attendance and assignment validation.

The service reads the active One Portal database through the existing table API.
It deliberately does not treat a missing attendance row as absence. For records on
an applicable work date, the latest InsertTS is authoritative; contradictory latest
records fail closed for new assignments.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Any, Iterable

from app.api import table_api

ATTENDANCE_TABLE = "Z_AttendanceInfo"
ATTENDANCE_ABSENT = "A"
ATTENDANCE_PRESENT = "P"
ATTENDANCE_NOT_MARKED = "NOT_MARKED"
ATTENDANCE_ERROR = "ERROR"


class EmployeeAvailabilityError(RuntimeError):
    """Raised when attendance/identity data cannot be safely resolved."""


class EmployeeAssignmentDenied(ValueError):
    """Raised when attendance rules prohibit a new employee assignment."""


def normalize_key(value: Any) -> str:
    text = _text(value)
    # The gateway sometimes serializes integer-looking identifiers as 139.0.
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.casefold() in {"", "none", "null", "nan", "nat", "n/a", "-"}:
        return ""
    return text


def _field(row: dict[str, Any] | None, *aliases: str) -> Any:
    if not isinstance(row, dict):
        return None
    alias_keys = {normalize_key(alias) for alias in aliases if alias}
    for key, value in row.items():
        if normalize_key(key) in alias_keys:
            return value
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse InsertTS and normalize timezone-aware values to UTC-naive for ordering."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        raw = _text(value)
        if not raw:
            return None
        raw = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is None:
            for fmt in (
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y%m%d %H%M%S",
            ):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def resolve_work_date(
    explicit: Any = None,
    payload: dict[str, Any] | None = None,
    current_row: dict[str, Any] | None = None,
) -> date:
    """Resolve the attendance date from the assignment being created/updated."""
    parsed = _parse_date(explicit)
    if parsed:
        return parsed

    candidates = (
        "I_TaskStartDateTime", "TaskStartDateTime", "TaskDate", "WorkDate",
        "S1_PLTimeStamp", "S2_PLTimeStamp", "S3_PLTimeStamp", "S4_PLTimeStamp",
        "S5_PLTimeStamp", "S6_PLTimeStamp", "PLTimestamp", "PlannedTimestamp",
        "S2_DispatchDateTime", "S3_DispatchDateTime", "S4_DispatchDateTime",
    )
    for source in (payload or {}, current_row or {}):
        for candidate in candidates:
            value = _field(source, candidate)
            parsed = _parse_date(value)
            if parsed:
                return parsed
    return date.today()


def _status_code(value: Any) -> str:
    normalized = _text(value).casefold()
    if normalized in {"a", "absent"}:
        return ATTENDANCE_ABSENT
    if normalized in {"p", "present"}:
        return ATTENDANCE_PRESENT
    return ""


def resolve_attendance_status(
    rows: Iterable[dict[str, Any]], employee_code: Any, work_date: date
) -> str:
    """Resolve A/P for one employee and work date from Z_AttendanceInfo rows.

    Missing daily records return NOT_MARKED (never inferred as Absent). Latest
    InsertTS wins. Ties or un-timestamped conflicting records are ambiguous.
    """
    target_code = normalize_key(employee_code)
    if not target_code:
        raise EmployeeAvailabilityError("EmployeeCode could not be resolved for attendance validation.")

    employee_rows: list[dict[str, Any]] = []
    matching_date_rows: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_code = _field(row, "EmployeeCode", "Employee Code")
        if normalize_key(row_code) != target_code:
            continue
        employee_rows.append(row)
        raw_date = _field(row, "AttendanceDate", "Attendance Date")
        parsed_date = _parse_date(raw_date)
        if not parsed_date:
            raise EmployeeAvailabilityError(
                f"Attendance record for employee {str(employee_code).strip()} has no valid AttendanceDate; assignment blocked."
            )
        if parsed_date == work_date:
            matching_date_rows.append(row)

    if not matching_date_rows:
        return ATTENDANCE_NOT_MARKED

    stamped: list[tuple[datetime, dict[str, Any]]] = []
    unstamped: list[dict[str, Any]] = []
    for row in matching_date_rows:
        raw_insert = _field(row, "InsertTS", "InsertTSDateTime", "InsertedAt", "CreatedAt")
        stamp = _parse_timestamp(raw_insert)
        if stamp is None:
            unstamped.append(row)
        else:
            stamped.append((stamp, row))

    if stamped:
        newest_stamp = max(stamp for stamp, _row in stamped)
        newest_rows = [row for stamp, row in stamped if stamp == newest_stamp]
        newest_statuses = {_status_code(_field(row, "Status")) for row in newest_rows}
        if "" in newest_statuses or len(newest_statuses) != 1:
            raise EmployeeAvailabilityError(
                f"Conflicting attendance records for employee {str(employee_code).strip()} on {work_date.isoformat()}; assignment blocked."
            )
        chosen = next(iter(newest_statuses))
        # An untimestamped status that conflicts with the newest stamped record
        # may be newer; do not silently guess which record is authoritative.
        for row in unstamped:
            older_or_equal_status = _status_code(_field(row, "Status"))
            if not older_or_equal_status or older_or_equal_status != chosen:
                raise EmployeeAvailabilityError(
                    f"Attendance records for employee {str(employee_code).strip()} on {work_date.isoformat()} cannot be ordered reliably; assignment blocked."
                )
        return chosen

    statuses = {_status_code(_field(row, "Status")) for row in matching_date_rows}
    if "" in statuses or len(statuses) != 1:
        raise EmployeeAvailabilityError(
            f"Conflicting attendance records for employee {str(employee_code).strip()} on {work_date.isoformat()}; assignment blocked."
        )
    return next(iter(statuses))


async def load_attendance_rows(employee_code: Any = None) -> list[dict[str, Any]]:
    """Load attendance records, using an employee filter for single assignments.

    If the gateway returns no rows for a filtered query, read the unfiltered table
    before concluding that no attendance record exists. This handles gateways that
    silently ignore/incorrectly implement the filter contract without treating an
    API mismatch as proof that the employee was present.
    """
    filtered_error: Exception | None = None
    if _text(employee_code):
        try:
            filtered = await table_api.get_table(
                ATTENDANCE_TABLE, filters={"EmployeeCode": _text(employee_code)}
            )
            if not isinstance(filtered, list):
                raise TypeError("filtered attendance response is not a row list")
            filtered_rows = [row for row in filtered if isinstance(row, dict)]
            requested_code = normalize_key(employee_code)
            if any(
                normalize_key(_field(row, "EmployeeCode", "Employee Code")) == requested_code
                for row in filtered_rows
            ):
                return filtered_rows
        except Exception as exc:
            filtered_error = exc

    try:
        rows = await table_api.get_table(ATTENDANCE_TABLE)
    except Exception as exc:
        cause = filtered_error or exc
        raise EmployeeAvailabilityError(
            "Attendance status could not be verified from Z_AttendanceInfo; assignment blocked."
        ) from cause
    if not isinstance(rows, list):
        raise EmployeeAvailabilityError(
            "Attendance data from Z_AttendanceInfo could not be read reliably; assignment blocked."
        ) from filtered_error
    return [row for row in rows if isinstance(row, dict)]


async def get_attendance_status_map(
    employee_codes: Iterable[Any], work_date: date | None = None,
) -> dict[str, str]:
    """Load attendance once and resolve each code independently.

    Per-employee conflicts are returned as ERROR so one bad row does not mislabel
    every employee in the dropdown. Database read errors still raise.
    """
    rows = await load_attendance_rows()
    actual_date = work_date or date.today()
    result: dict[str, str] = {}
    for code in employee_codes:
        normalized = normalize_key(code)
        if not normalized:
            continue
        try:
            result[normalized] = resolve_attendance_status(rows, code, actual_date)
        except EmployeeAvailabilityError:
            result[normalized] = ATTENDANCE_ERROR
    return result


async def get_employee_attendance_status(employee_code: Any, work_date: date | None = None) -> str:
    rows = await load_attendance_rows(employee_code)
    return resolve_attendance_status(rows, employee_code, work_date or date.today())


async def ensure_employee_not_absent(employee_code: Any, work_date: date | None = None) -> str:
    """Fail closed on unreadable/contradictory attendance, and reject Status A."""
    status = await get_employee_attendance_status(employee_code, work_date)
    if status == ATTENDANCE_ABSENT:
        raise EmployeeAssignmentDenied(
            "This employee is marked Absent in Z_AttendanceInfo and cannot be assigned work."
        )
    if status == ATTENDANCE_ERROR:
        raise EmployeeAvailabilityError(
            "Attendance records are conflicting or cannot be resolved for this employee and work date; assignment blocked."
        )
    return status


def employee_identity_maps(employee_rows: Iterable[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """Map IDs and names to EmployeeCode; mark duplicate bare names as ambiguous."""
    identities: dict[str, str] = {}
    names: dict[str, str] = {}
    for row in employee_rows or []:
        if not isinstance(row, dict):
            continue
        code = _text(_field(row, "EmployeeCode", "Employee Code"))
        if not code:
            continue
        identities[normalize_key(code)] = code
        employee_id = _text(_field(row, "EmployeeID", "Employee ID", "EmpID", "EmployeeInternalID"))
        if employee_id:
            identities[normalize_key(employee_id)] = code
        employee_name = _text(_field(row, "EmployeeName", "Employee Name", "Name", "FullName"))
        if employee_name:
            name_key = normalize_key(employee_name)
            existing = names.get(name_key)
            names[name_key] = code if existing in (None, code) else ""
            # A display label carrying the code remains uniquely resolvable even
            # when two employees share the same bare name.
            names[normalize_key(f"{employee_name} ({code})")] = code
    return identities, names


async def resolve_employee_code(value: Any, *, employee_rows: list[dict[str, Any]] | None = None) -> str:
    """Resolve an employee code or exact master name/ID to EmployeeCode."""
    raw = _text(value)
    if not raw:
        return ""
    if employee_rows is None:
        try:
            employee_rows = await table_api.get_table("VM_EmployeeMaster")
        except Exception as exc:
            raise EmployeeAvailabilityError("Employee master could not be read for attendance validation; assignment blocked.") from exc
    identity_map, name_map = employee_identity_maps(employee_rows or [])
    key = normalize_key(raw)
    if key in identity_map:
        return identity_map[key]
    if key in name_map:
        return name_map[key]
    # Handle typical display values such as "Name (EmployeeCode)".
    match = re.search(r"\(([^()]+)\)\s*$", raw)
    if match:
        code = normalize_key(match.group(1))
        if code in identity_map:
            return identity_map[code]
    return ""


def _assignment_kind(field_name: Any) -> str:
    """Recognize employee assignee fields without treating contacts/audit fields as assignments."""
    key = normalize_key(field_name)
    if not key:
        return ""
    # Never interpret audit metadata or contact details as a work assignment.
    if any(token in key for token in (
        "createdby", "updatedby", "modifiedby", "enteredby", "approvedby", "markedby",
        "contact", "mobile", "phone", "email", "landline",
    )):
        return ""

    # Pickers require their PickID-based Busy validation in addition to attendance.
    if (
        key in {"s1picker", "picker", "pickername", "assignedpicker", "pickeremployee", "pickeremployeecode"}
        or key.endswith("picker") or key.endswith("pickername") or key.endswith("pickeremployeecode")
    ):
        return "picker"
    if key.endswith("doercode") or key.endswith("doerempcode") or key.endswith("doeremployeeid"):
        return "doer_code"
    if key.endswith("assigneddriver") or key.endswith("driveremployee") or key.endswith("driveremployeecode") or key.endswith("assigneddrivercode"):
        return "driver"
    if key.endswith("assignedrunner") or key.endswith("runneremployeecode") or key in {"irunner", "irunneremployeecode", "runner", "runnername"}:
        return "employee"
    if key.endswith("doer") or key.endswith("doername") or key.endswith("assignedto") or key.endswith("assignedemployee") or key.endswith("assignedemployeecode") or key.endswith("assignedemployeeid"):
        return "employee"

    # Standard role/assignee fields used by dynamic stages and custom forms.
    role_markers = ("doer", "runner", "driver", "picker", "assignee", "assignedto", "assignedemployee", "technician", "operator", "worker", "courier", "resource")
    if any(marker in key for marker in role_markers):
        if key.endswith(("employeecode", "employeeid", "doercode", "driverid", "userid", "code")):
            return "driver" if "driver" in key else ("doer_code" if "doer" in key else "employee")
        if key.endswith(("name", "employee", "assignedto", "assignee", "runner", "driver", "picker", "doer", "worker", "operator", "technician", "courier", "resource")):
            return "driver" if "driver" in key else ("picker" if "picker" in key else "employee")
    return ""


def _find_current_value(row: dict[str, Any], field_name: str) -> Any:
    return _field(row, field_name)


def _same_value(a: Any, b: Any) -> bool:
    return normalize_key(a) == normalize_key(b)


def _parse_assignment_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        source = list(value)
    else:
        source = str(value or "").split(",")
    seen: set[str] = set()
    result: list[str] = []
    for item in source:
        text = _text(item)
        key = normalize_key(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


async def validate_new_assignment_changes(
    payload: dict[str, Any],
    current_row: dict[str, Any] | None,
    *,
    work_date: date | None = None,
) -> None:
    """Validate changed employee-assignment fields before they are persisted.

    Existing assignments are not revalidated merely because someone completes a
    stage. Clearing an assignment is allowed. Picker lists are validated by the
    Pick/Pack-specific service because that path also checks Picker Busy rules.
    """
    row = current_row if isinstance(current_row, dict) else {}
    changes: list[tuple[str, str, Any]] = []
    for field_name, new_value in (payload or {}).items():
        kind = _assignment_kind(field_name)
        if not kind or kind == "picker":
            continue
        old_value = _find_current_value(row, str(field_name))
        if _same_value(new_value, old_value):
            continue
        if not _text(new_value):
            continue
        changes.append((str(field_name), kind, new_value))

    if not changes:
        return

    actual_date = resolve_work_date(work_date, payload, row)
    try:
        employee_rows_raw = await table_api.get_table("VM_EmployeeMaster")
    except Exception as exc:
        raise EmployeeAvailabilityError(
            "Employee master could not be read for attendance validation; assignment blocked."
        ) from exc
    employee_rows = [row for row in (employee_rows_raw or []) if isinstance(row, dict)]

    for field_name, kind, value in changes:
        if kind == "driver":
            # Third-party drivers are free-text contacts rather than employees.
            vehicle_origin = _field(payload, "S2_VehicleOrigin", "VehicleOrigin")
            if not _text(vehicle_origin):
                vehicle_origin = _field(row, "S2_VehicleOrigin", "VehicleOrigin")
            origin_key = normalize_key(vehicle_origin)
            if origin_key in {"thirdparty", "thirdpartyvehicle", "external", "externalvehicle"}:
                continue

        employee_code = await resolve_employee_code(value, employee_rows=employee_rows)
        if not employee_code and kind == "driver":
            # Resolve an internal driver by FL_Drivers.user_id -> FL_Trips.driver_id
            # style identity, then FL_Drivers.employee_id -> VM_EmployeeMaster ID.
            try:
                drivers = await table_api.get_table("FL_Drivers")
            except Exception as exc:
                raise EmployeeAvailabilityError(
                    "Driver identity could not be verified for attendance; assignment blocked."
                ) from exc
            identity_map, _name_map = employee_identity_maps(employee_rows)
            for driver in drivers or []:
                if not isinstance(driver, dict):
                    continue
                driver_name = _field(driver, "name", "driver_name", "EmployeeName", "FullName", "UserName")
                if normalize_key(driver_name) != normalize_key(value):
                    continue
                employee_id = _field(driver, "employee_id", "EmployeeID", "Employee ID", "EmpID")
                employee_code = identity_map.get(normalize_key(employee_id), "")
                if employee_code:
                    break
            if not employee_code:
                raise EmployeeAvailabilityError(
                    "The selected internal driver could not be mapped to VM_EmployeeMaster; assignment blocked."
                )
        elif not employee_code:
            raise EmployeeAvailabilityError(
                f"The employee selected in {field_name} could not be matched to VM_EmployeeMaster; assignment blocked."
            )

        old_value = _find_current_value(row, field_name)
        if _text(old_value):
            old_code = await resolve_employee_code(old_value, employee_rows=employee_rows)
            if old_code and normalize_key(old_code) == normalize_key(employee_code):
                # The form may serialize a display label differently from the
                # stored name/code. This is still the same existing assignment.
                continue

        await ensure_employee_not_absent(employee_code, actual_date)


def _has_timestamp(value: Any) -> bool:
    return _text(value).casefold() not in {"", "none", "null", "nan", "nat", "-"}


async def get_busy_employee_codes(employee_rows: list[dict[str, Any]] | None = None) -> set[str]:
    """Return employee-code keys having an open KPI task or an active trip.

    Uses both KPI table spellings present in app integrations (KPI_MasterRecord
    and Z_KPI_MasterRecord). A read failure across all KPI variants, or a failure
    to read trip/driver mappings, raises instead of silently making everyone free.
    """
    if employee_rows is None:
        try:
            employee_rows = await table_api.get_table("VM_EmployeeMaster")
        except Exception as exc:
            raise EmployeeAvailabilityError("Employee master could not be read for Busy validation.") from exc
    employee_rows = [row for row in (employee_rows or []) if isinstance(row, dict)]
    identity_map, name_map = employee_identity_maps(employee_rows)
    busy: set[str] = set()

    kpi_rows: list[dict[str, Any]] = []
    successful_kpi_reads = 0
    kpi_errors: list[Exception] = []
    # Z_KPI_MasterRecord is used by the existing Runner360 Busy implementation;
    # KPI_MasterRecord is also part of the documented Pick/Dispatch rule.
    for table_name in ("KPI_MasterRecord", "Z_KPI_MasterRecord"):
        try:
            rows = await table_api.get_table(table_name)
            successful_kpi_reads += 1
            kpi_rows.extend(row for row in (rows or []) if isinstance(row, dict))
        except Exception as exc:
            kpi_errors.append(exc)
    if successful_kpi_reads != 2:
        # These two table names cover the documented KPI condition and the
        # existing Runner360 Busy source. Partial success is not enough to safely
        # conclude that an employee is Available.
        raise EmployeeAvailabilityError(
            "KPI Busy status could not be fully verified from KPI_MasterRecord and Z_KPI_MasterRecord; assignment blocked."
        ) from (kpi_errors[0] if kpi_errors else None)

    # De-duplicate the same KPI row when both table names point to a shared view.
    seen_kpi: set[tuple[str, str, str, str]] = set()
    for row in kpi_rows:
        planned = _field(row, "PLTimestamp", "PLTimeStamp", "PlannedTimestamp", "PlannedTimeStamp", "S1_PLTimeStamp")
        actual = _field(row, "ALTimestamp", "ALTimeStamp", "ActualTimestamp", "ActualTimeStamp", "S1_ALTimeStamp")
        if not _has_timestamp(planned) or _has_timestamp(actual):
            continue
        # KPI_MasterRecord uses DoerCode; some existing Z_KPI_MasterRecord views
        # expose the same identity as Doer/Runner/AssignedTo instead.
        doer_values = [
            _field(row, "DoerCode", "Doer Code", "EmployeeCode", "Employee Code", "EmployeeID", "Employee ID"),
            _field(row, "Doer", "S1_Doer", "Runner", "RunnerName", "AssignedTo", "AssignedEmployee", "EmployeeName"),
        ]
        resolved_codes: set[str] = set()
        for doer_value in doer_values:
            if not _text(doer_value):
                continue
            raw = _text(doer_value)
            key = normalize_key(raw)
            resolved = identity_map.get(key) or name_map.get(key)
            if not resolved:
                match = re.search(r"\(([^()]+)\)\s*$", raw)
                if match:
                    resolved = identity_map.get(normalize_key(match.group(1)), "")
            if not resolved and key in name_map:
                raise EmployeeAvailabilityError(
                    "A KPI task's employee identity matches multiple employees by name; Busy status cannot be verified safely."
                )
            # Some KPI tables store EmployeeCode directly even if master rows are
            # restricted; preserving the exact value lets same-code matching work.
            resolved = resolved or raw
            resolved_codes.add(normalize_key(resolved))
        for resolved_key in resolved_codes:
            if not resolved_key:
                continue
            signature = (
                resolved_key,
                _text(planned),
                _text(actual),
                normalize_key(_field(row, "RowID", "ID", "PK")),
            )
            if signature in seen_kpi:
                continue
            seen_kpi.add(signature)
            busy.add(resolved_key)

    try:
        drivers = await table_api.get_table("FL_Drivers")
        trips = await table_api.get_table("FL_Trips")
    except Exception as exc:
        raise EmployeeAvailabilityError("Active trip status could not be verified; assignment blocked.") from exc

    driver_code_by_user_id: dict[str, str] = {}
    for driver in drivers or []:
        if not isinstance(driver, dict):
            continue
        user_id = normalize_key(_field(driver, "user_id", "UserID", "userId"))
        employee_id = _field(driver, "employee_id", "EmployeeID", "Employee ID", "EmpID")
        direct_code = _field(driver, "EmployeeCode", "Employee Code")
        employee_code = identity_map.get(normalize_key(employee_id), "")
        if not employee_code and direct_code:
            employee_code = identity_map.get(normalize_key(direct_code), _text(direct_code))
        if user_id and employee_code:
            driver_code_by_user_id[user_id] = employee_code

    for trip in trips or []:
        if not isinstance(trip, dict):
            continue
        actual_start = _field(trip, "actual_start_time", "ActualStartTime", "Actual Start Time")
        completion = _field(trip, "completion_time", "CompletionTime", "Completion Time")
        if not _has_timestamp(actual_start) or _has_timestamp(completion):
            continue
        driver_id = normalize_key(_field(trip, "driver_id", "DriverID", "Driver ID"))
        employee_code = driver_code_by_user_id.get(driver_id, "")
        if employee_code:
            busy.add(normalize_key(employee_code))

    return busy
