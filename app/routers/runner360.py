"""



Runner360 routes.



This module handles the Runner360 Team Lead input form and creates the



corresponding Z_Runner360 task through the portal's existing metadata-driven



table API.



Runner360 uses:



    FMS Name : Runner360



    App ID   : APP000014



    Table    : Z_Runner360



    Stage    : S1



"""



from __future__ import annotations



from datetime import date, datetime, time as clock_time, timedelta
import ipaddress
import math
import os
import logging
import base64
import hashlib
import hmac
import json
import time


from typing import Any



from fastapi import APIRouter, HTTPException, Request



from fastapi.responses import HTMLResponse, JSONResponse



from app.api import table_api
from app.config import settings



from app.api.client import api_client
from app.auth.request_context import get_current_database_target



from app.services import dashboard_service, metadata_service, stage_service
from app.utils.delay_calculator import parse_datetime
from app.services.whatsapp_service import send_runner360_task_notification



from app.templates_config import templates



router = APIRouter(tags=["runner360"])
logger = logging.getLogger("fms_portal.runner360")


MOBILE_CHECKIN_TTL_SECONDS = 7 * 24 * 60 * 60


def _mobile_checkin_secret() -> bytes:
    """Use the existing SSO secret to sign time-limited public task links."""
    secret = str(settings.fms_sso_secret or settings.jwt_secret_key or "").strip()
    if len(secret) < 32:
        raise ValueError("FMS_SSO_SECRET (or JWT_SECRET_KEY) must contain at least 32 characters to sign mobile Check In links.")
    return secret.encode("utf-8")


def _create_mobile_checkin_token(task_id: str, employee_code: str) -> str:
    payload = {
        "task_id": str(task_id),
        "employee_code": str(employee_code),
        "exp": int(time.time()) + MOBILE_CHECKIN_TTL_SECONDS,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    signature = hmac.new(_mobile_checkin_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded}.{encoded_signature}"


def _read_mobile_checkin_token(token: str) -> dict[str, Any]:
    try:
        encoded, supplied_signature = str(token or "").split(".", 1)
        expected_signature = base64.urlsafe_b64encode(
            hmac.new(_mobile_checkin_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("Invalid Check In link signature.")
        payload_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid Check In link.")
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError("This Check In link has expired. Ask your manager to resend the task notification.")
        if not str(payload.get("task_id") or "").strip() or not str(payload.get("employee_code") or "").strip():
            raise ValueError("Invalid Check In link details.")
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) and str(exc) else "This Check In link is invalid. Ask your manager to resend the task notification."
        raise HTTPException(status_code=401, detail=message) from exc


def _mobile_checkin_base_url(request: Request) -> str:
    configured = str(settings.public_base_url or "").strip().rstrip("/")
    if configured and "localhost" not in configured.casefold() and "127.0.0.1" not in configured:
        return configured
    request_url = str(request.base_url).strip().rstrip("/")
    if settings.is_production and request_url.startswith("http://"):
        request_url = "https://" + request_url[len("http://"):]
    return request_url




RUNNER360_APP_ID = "APP000014"



RUNNER360_FMS_NAME = "Runner360"



RUNNER360_TABLE = "Z_Runner360"



RUNNER360_STAGE_NO = "S1"



RUNNER360_STAGE_NAME = "S1_Runner_Return"





def _text(value: Any) -> str:



    if value is None:



        return ""



    return str(value).strip()



def _normalise(value: Any) -> str:



    return " ".join(_text(value).casefold().split())



def _column_key(value: Any) -> str:

    """Normalize a column/value key without depending on casing or separators."""

    return "".join(ch for ch in _text(value).casefold() if ch.isalnum())





def _lookup_case_insensitive(

    row: dict[str, Any],

    *names: str,

) -> Any:

    """Find a row value while tolerating spaces, underscores, and casing."""

    wanted = {_column_key(name) for name in names}

    for key, value in row.items():

        if _column_key(key) in wanted:

            return value

    return None





def _parse_holiday_date(value: Any) -> date | None:
    """Parse a VM_HolidaysMaster date value from common API/SQL date formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    if not raw or raw.casefold() in {"none", "null", "-"}:
        return None
    # SQL Server API normally sends ISO timestamps, e.g. 2026-10-13 00:00:00.000.
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


async def _get_runner360_holiday_ranges() -> list[dict[str, str]]:
    """Read company holiday ranges from VM_HolidaysMaster (StrDate through EndDate)."""
    try:
        rows = await table_api.get_table("VM_HolidaysMaster")
    except Exception as exc:
        print(f"[Runner360 holidays] Failed to load VM_HolidaysMaster: {exc}")
        raise HTTPException(
            status_code=502,
            detail="Unable to load company holidays from VM_HolidaysMaster. Refresh and try again.",
        ) from exc

    holidays: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        start_value = _lookup_case_insensitive(row, "StrDate", "StartDate", "Start Date", "HolidayDate", "Date")
        end_value = _lookup_case_insensitive(row, "EndDate", "End Date")
        start_date = _parse_holiday_date(start_value)
        if not start_date:
            continue
        end_date = _parse_holiday_date(end_value) or start_date
        if end_date < start_date:
            end_date = start_date
        remarks = _text(_lookup_case_insensitive(row, "Rmrks", "Remarks", "Remark", "Description", "HolidayName"))
        holidays.append({
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "remarks": remarks,
        })
    return holidays


async def _validate_runner360_working_date(task_date_value: str) -> None:
    """Reject past dates, Sundays, and company holiday dates on the server as well as in the UI."""
    selected_date = _parse_holiday_date(task_date_value)
    if selected_date is None:
        raise HTTPException(status_code=400, detail="Task Start Date must be a valid date.")
    if selected_date < date.today():
        raise HTTPException(status_code=400, detail="Past dates are unavailable for Runner360 assignments.")
    if selected_date.weekday() == 6:
        raise HTTPException(status_code=400, detail="Sunday is treated as a holiday. Choose another task date.")
    for holiday in await _get_runner360_holiday_ranges():
        start_date = date.fromisoformat(holiday["start_date"])
        end_date = date.fromisoformat(holiday["end_date"])
        if start_date <= selected_date <= end_date:
            label = holiday.get("remarks") or "Company holiday"
            raise HTTPException(status_code=400, detail=f"{label} is listed in VM_HolidaysMaster. Choose another task date.")


def _branch_key(value: Any) -> str:

    """Treat legacy labels such as 'Noida Location' as the master branch 'Noida'."""

    key = _column_key(value)

    if key.endswith("location"):

        key = key[:-len("location")]

    return key





def _is_active(value: Any) -> bool:

    return _normalise(value) in {"y", "yes", "1", "true", "active"}


def _has_timestamp_value(value: Any) -> bool:
    """Treat database NULL-like strings as empty timestamps."""
    return _text(value).casefold() not in {"", "none", "null", "nan", "nat", "-"}


def _find_timestamp_value(row: dict[str, Any], exact_names: tuple[str, ...], suffixes: tuple[str, ...]) -> Any:
    """Find an exact timestamp column first, then tolerate stage-prefixed names."""
    keyed = {_column_key(key): value for key, value in row.items()}
    for name in exact_names:
        key = _column_key(name)
        if key in keyed:
            return keyed[key]
    for key, value in row.items():
        normalized = _column_key(key)
        if any(normalized.endswith(suffix) for suffix in suffixes):
            return value
    return None


def _runner_identity_values(row: dict[str, Any]) -> list[Any]:
    """Return fields most likely to identify the assigned doer/runner in the KPI master."""
    primary_markers = ("doer", "runner", "assignee", "assignedto", "resource", "operator")
    employee_markers = ("employeecode", "employeeid", "employeename", "employee")
    excluded_markers = ("createdby", "updatedby", "modifiedby", "enteredby", "approvedby")

    primary: list[Any] = []
    employee: list[Any] = []
    for key, value in row.items():
        normalized = _column_key(key)
        if not normalized or any(marker in normalized for marker in excluded_markers):
            continue
        if not _has_timestamp_value(value):
            continue
        if any(marker in normalized for marker in primary_markers):
            primary.append(value)
        elif any(marker in normalized for marker in employee_markers):
            employee.append(value)

    if primary:
        return primary
    if employee:
        return employee
    # Some versions expose a less descriptive identity-column name. In that case,
    # exact equality against the known employee name/code remains safe enough.
    return [value for value in row.values() if _has_timestamp_value(value)]


def _identity_value_matches_assignment(value: Any, assignment: dict[str, Any]) -> bool:
    """Match a KPI master identity value against a runner's name or employee code."""
    raw = _text(value)
    if not raw:
        return False
    candidates = {raw, raw.split("(", 1)[0].strip(), raw.split("[", 1)[0].strip()}
    identity_keys = {_column_key(item) for item in candidates if item}
    wanted = {
        _column_key(assignment.get("employee_code")),
        _column_key(assignment.get("employee_name")),
    }
    wanted.discard("")
    return bool(identity_keys & wanted)


def _row_has_open_planned_task_for_runner(row: dict[str, Any], assignment: dict[str, Any]) -> bool:
    """A runner is busy when KPI master has a planned timestamp but no actual timestamp."""
    planned = _find_timestamp_value(
        row,
        ("PLTimeStamp", "PlannedTimestamp", "PlannedTimeStamp", "PLTimestamp", "S1_PLTimeStamp"),
        ("pltimestamp", "plannedtimestamp", "plannedtime"),
    )
    actual = _find_timestamp_value(
        row,
        ("ALTimeStamp", "ActualTimestamp", "ActualTimeStamp", "S1_ALTimeStamp", "ActualDateTime"),
        ("altimestamp", "actualtimestamp", "actualdatetime", "actualtime"),
    )
    if not _has_timestamp_value(planned) or _has_timestamp_value(actual):
        return False
    return any(_identity_value_matches_assignment(value, assignment) for value in _runner_identity_values(row))


async def _get_runner_busy_status(assignments: list[dict[str, Any]]) -> None:
    """Mark runners busy using open planned rows in Z_KPI_MasterRecord."""
    try:
        master_rows = await table_api.get_table("Z_KPI_MasterRecord")
    except Exception as exc:
        print(f"[Runner360 busy status] Failed to load Z_KPI_MasterRecord: {exc}")
        raise HTTPException(
            status_code=502,
            detail="Unable to check runner availability in Z_KPI_MasterRecord. Refresh and try again.",
        ) from exc

    for assignment in assignments:
        assignment["is_busy"] = any(
            isinstance(row, dict) and _row_has_open_planned_task_for_runner(row, assignment)
            for row in (master_rows or [])
        )


async def _get_runner_assignments() -> list[dict[str, Any]]:

    """

    Dynamically load runner assignments from VM_Team and VM_EmployeeMaster.



    No employee names, branches, or TeamIDs are embedded in the application.

    VM_Team identifies membership in a team whose TeamName contains 'Runner';

    VM_EmployeeMaster supplies active status, canonical employee name, and branch.

    EmployeeCode is the primary join key, with EmployeeID as a data fallback.

    """

    try:

        team_rows = await table_api.get_table("VM_Team")

        employee_rows = await table_api.get_table("VM_EmployeeMaster")

    except Exception as exc:

        print(f"[Runner360] Failed to load VM_Team/VM_EmployeeMaster: {exc}")

        raise HTTPException(

            status_code=502,

            detail="Unable to load Runner360 team and employee master data.",

        ) from exc

    employees_by_code: dict[str, dict[str, Any]] = {}

    employees_by_id: dict[str, dict[str, Any]] = {}

    for employee in employee_rows or []:

        if not isinstance(employee, dict):

            continue

        if not _is_active(_lookup_case_insensitive(employee, "Active")):

            continue

        name = _text(_lookup_case_insensitive(employee, "EmployeeName", "Employee Name", "Name"))

        branch = _text(_lookup_case_insensitive(employee, "Branch", "EmployeeBranch"))

        if not name or not branch:

            continue

        code = _text(_lookup_case_insensitive(employee, "EmployeeCode", "Employee Code"))

        emp_id = _text(_lookup_case_insensitive(employee, "EmployeeID", "Employee ID", "EmpID"))

        if code:

            employees_by_code[_column_key(code)] = employee

        if emp_id:

            employees_by_id[_column_key(emp_id)] = employee



    assignments: list[dict[str, str]] = []

    seen: set[tuple[str, str]] = set()

    for team in team_rows or []:

        if not isinstance(team, dict):

            continue

        team_name = _text(_lookup_case_insensitive(team, "TeamName", "Team Name"))

        if "runner" not in _column_key(team_name):

            continue



        team_code = _text(_lookup_case_insensitive(team, "EmployeeCode", "Employee Code"))

        team_emp_id = _text(_lookup_case_insensitive(team, "EmployeeID", "Employee ID", "EmpID"))

        employee = employees_by_code.get(_column_key(team_code)) if team_code else None

        if employee is None and team_emp_id:

            employee = employees_by_id.get(_column_key(team_emp_id))

        if not employee:

            continue



        employee_code = _text(_lookup_case_insensitive(employee, "EmployeeCode", "Employee Code"))

        employee_name = _text(_lookup_case_insensitive(employee, "EmployeeName", "Employee Name", "Name"))

        branch = _text(_lookup_case_insensitive(employee, "Branch", "EmployeeBranch"))

        team_id = _text(_lookup_case_insensitive(team, "TeamID", "Team ID"))

        if not employee_code or not employee_name or not branch:

            continue



        dedupe_key = (_branch_key(branch), _column_key(employee_code))

        if dedupe_key in seen:

            continue

        seen.add(dedupe_key)

        assignments.append({

            "branch": branch,

            "team_id": team_id,

            "team_name": team_name,

            "employee_code": employee_code,

            "employee_name": employee_name,

            # Used only when live-recipient mode is explicitly enabled. Test mode
            # always sends to WHATSAPP_TEST_NUMBER instead.
            "mobile_number": _text(_lookup_case_insensitive(
                employee, "PersonalMobile", "Personal Mobile", "Mobile", "OffMobile",
                "MobileNo", "MobileNumber", "Phone", "PhoneNo", "PhoneNumber",
                "ContactNo", "ContactNumber",
            )),

            # Working hours come from the employee master for this assigned runner.
            "shift_start_time": _text(_lookup_case_insensitive(employee, "ShiftStartTime", "Shift Start Time")),

            "shift_end_time": _text(_lookup_case_insensitive(employee, "ShiftEndTime", "Shift End Time")),

            # Lunch is intentionally not subtracted from the working window: the geofence
            # stays enforced through lunch, as requested.
            "lunch_start_time": _text(_lookup_case_insensitive(employee, "LunchStartTime", "Lunch Start Time")),

            "lunch_end_time": _text(_lookup_case_insensitive(employee, "LunchEndTime", "Lunch End Time")),

        })



    assignments.sort(key=lambda item: (item["branch"].casefold(), item["employee_name"].casefold()))

    return assignments


_RUNNER360_MOBILE_FIELD_ALIASES = (
    "PersonalMobile", "Personal Mobile", "Mobile", "OffMobile",
    "MobileNo", "MobileNumber", "Phone", "PhoneNo", "PhoneNumber",
    "ContactNo", "ContactNumber",
)


async def _get_runner360_mobile_by_employee_code(employee_code: str) -> str:
    """Re-fetch one exact employee row when the full master list has a blank mobile.

    Some gateway responses return blank contact fields in the unfiltered list while
    a direct EmployeeCode lookup returns the full master row. Never accept a mobile
    value from another employee if the gateway ignores the filter.
    """
    target_code = _column_key(employee_code)
    if not target_code:
        return ""
    try:
        rows = await table_api.get_table(
            "VM_EmployeeMaster", filters={"EmployeeCode": employee_code}
        )
    except Exception as exc:
        logger.warning(
            "Runner360 targeted mobile lookup failed: target_database=%s employee_code=%s error_type=%s",
            get_current_database_target() or "environment-default", employee_code, type(exc).__name__
        )
        return ""

    for employee in rows or []:
        if not isinstance(employee, dict):
            continue
        returned_code = _text(_lookup_case_insensitive(employee, "EmployeeCode", "Employee Code"))
        if _column_key(returned_code) != target_code:
            continue
        mobile = _text(_lookup_case_insensitive(employee, *_RUNNER360_MOBILE_FIELD_ALIASES))
        if mobile:
            digits = "".join(ch for ch in mobile if ch.isdigit())
            logger.info(
                "Runner360 targeted mobile lookup: target_database=%s employee_code=%s found=%s digit_count=%d",
                get_current_database_target() or "environment-default", employee_code, bool(mobile), len(digits)
            )
            return mobile
    logger.warning(
        "Runner360 targeted mobile lookup returned no populated mobile: target_database=%s employee_code=%s rows=%d",
        get_current_database_target() or "environment-default", employee_code, len(rows or [])
    )
    return ""


# These are common names used for the employee's immediate reporting-manager
# reference in VM_EmployeeMaster. The reference can contain an EmployeeCode,
# EmployeeID, or an employee name; the resolver below supports all three.
# VM_EmployeeMaster columns used to resolve the employee reporting hierarchy.
# ManagerEC is the preferred relationship key shown in the company master view;
# Manager is retained as a readable-name fallback when a manager code is absent.
_RUNNER360_MANAGER_CODE_COLUMNS = (
    "ManagerEC", "Manager EC", "ManagerEmployeeCode", "Manager Employee Code",
    "ManagerCode", "Manager Code", "ReportingManagerEmployeeCode",
    "Reporting Manager Employee Code", "ReportingToEmployeeCode",
    "Reporting To Employee Code", "ReportsToEmployeeCode", "Reports To Employee Code",
    "SupervisorEmployeeCode", "Supervisor Employee Code", "ImmediateManagerCode",
    "Immediate Manager Code",
)

_RUNNER360_MANAGER_NAME_COLUMNS = (
    "Manager", "ManagerName", "Manager Name", "ReportingManager",
    "Reporting Manager", "ReportingManagerName", "Reporting Manager Name",
    "ReportingTo", "Reporting To", "ReportsTo", "Reports To", "ReportTo",
    "Report To", "Supervisor", "SupervisorName", "Supervisor Name",
    "ImmediateManager", "Immediate Manager", "ReportingPerson",
    "Reporting Person", "ReportingHead", "Reporting Head",
)

_RUNNER360_REPORTING_COLUMNS = (
    *_RUNNER360_MANAGER_CODE_COLUMNS,
    *_RUNNER360_MANAGER_NAME_COLUMNS,
    "ManagerEmployeeID", "Manager Employee ID", "ManagerID", "Manager ID",
    "ReportingManagerID", "Reporting Manager ID", "ReportingToEmployeeID",
    "Reporting To Employee ID", "SupervisorID", "Supervisor ID",
)


def _employee_master_key(row: dict[str, Any]) -> str:
    """Return a stable key for an employee row, preferring EmployeeCode."""
    value = _lookup_case_insensitive(row, "EmployeeCode", "Employee Code")
    if not _text(value):
        value = _lookup_case_insensitive(row, "EmployeeID", "Employee ID", "EmpID")
    if not _text(value):
        value = _lookup_case_insensitive(row, "EmployeeName", "Employee Name", "Name")
    return _column_key(value)


def _resolve_descendant_employee_codes(
    employee_rows: list[dict[str, Any]], current_employee_code: str
) -> set[str]:
    """Return all direct and indirect reports of the logged-in employee.

    The hierarchy is derived from VM_EmployeeMaster. ManagerEC is the preferred
    parent link; Manager name is used only when the manager code cannot resolve.
    No department, branch, or employee names are hardcoded. Cycles and ambiguous
    name matches are ignored safely.
    """
    rows_by_key: dict[str, dict[str, Any]] = {}
    identity_index: dict[str, str | None] = {}
    code_to_key: dict[str, str] = {}
    reporting_keys = {_column_key(name) for name in _RUNNER360_REPORTING_COLUMNS}
    reporting_column_found = False

    def add_identity_token(value: Any, key: str) -> None:
        for token in {_column_key(value), _normalise(value)}:
            if not token:
                continue
            if token in identity_index and identity_index[token] != key:
                identity_index[token] = None  # ambiguous token: never guess
            else:
                identity_index[token] = key

    for row in employee_rows or []:
        if not isinstance(row, dict):
            continue
        key = _employee_master_key(row)
        if not key:
            continue
        rows_by_key[key] = row
        if any(_column_key(column) in reporting_keys for column in row.keys()):
            reporting_column_found = True

        code = _text(_lookup_case_insensitive(row, "EmployeeCode", "Employee Code"))
        emp_id = _text(_lookup_case_insensitive(row, "EmployeeID", "Employee ID", "EmpID"))
        name = _text(_lookup_case_insensitive(row, "EmployeeName", "Employee Name", "Name"))
        if code:
            code_to_key[_column_key(code)] = key
        for value in (code, emp_id, name):
            add_identity_token(value, key)

    if not reporting_column_found:
        raise HTTPException(
            status_code=503,
            detail=(
                "Runner hierarchy cannot be applied because VM_EmployeeMaster does not "
                "expose a recognized ManagerEC/Manager reporting column."
            ),
        )

    current_key = code_to_key.get(_column_key(current_employee_code))
    if not current_key:
        raise HTTPException(
            status_code=401,
            detail="Your authenticated employee was not found in VM_EmployeeMaster.",
        )

    parent_by_child: dict[str, str] = {}
    for child_key, row in rows_by_key.items():
        manager_key = None

        # The screenshot confirms ManagerEC is available. Resolve by employee code
        # first because it is stable even when employee names are duplicated/changed.
        manager_code = next((
            candidate
            for alias in _RUNNER360_MANAGER_CODE_COLUMNS
            for candidate in [_lookup_case_insensitive(row, alias)]
            if _text(candidate)
        ), None)
        if _text(manager_code):
            manager_key = code_to_key.get(_column_key(manager_code))
            if not manager_key:
                manager_key = identity_index.get(_column_key(manager_code))
                if manager_key is None:
                    manager_key = identity_index.get(_normalise(manager_code))

        # Fall back to manager name / other reporting-name columns only if the
        # manager code did not resolve to a known employee.
        if not manager_key:
            manager_name = None
            for alias in _RUNNER360_MANAGER_NAME_COLUMNS:
                candidate = _lookup_case_insensitive(row, alias)
                if _text(candidate):
                    manager_name = candidate
                    break
            if _text(manager_name):
                manager_key = identity_index.get(_normalise(manager_name))
                if manager_key is None:
                    manager_key = identity_index.get(_column_key(manager_name))

        if manager_key and manager_key != child_key:
            parent_by_child[child_key] = manager_key

    children: dict[str, list[str]] = {}
    for child_key, manager_key in parent_by_child.items():
        children.setdefault(manager_key, []).append(child_key)

    descendants: set[str] = set()
    visited = {current_key}
    stack = [current_key]
    while stack:
        manager_key = stack.pop()
        for child_key in children.get(manager_key, []):
            if child_key in visited:
                continue
            visited.add(child_key)
            child = rows_by_key.get(child_key, {})
            code = _text(_lookup_case_insensitive(child, "EmployeeCode", "Employee Code"))
            if code:
                descendants.add(_column_key(code))
            stack.append(child_key)

    return descendants


async def _visible_runner_codes_for_request(request: Request) -> set[str] | None:
    """Return descendant employee codes for authenticated users.

    A `None` result is reserved for unauthenticated local/Guest development mode,
    where there is no employee identity from which to derive a reporting tree.
    Role ID 1 retains the existing administrator-wide view.
    """
    employee_code = _text(getattr(request.state, "employee_code", ""))
    if not employee_code:
        return None

    # Preserve the portal's established administrator role as a company-wide viewer.
    if _text(getattr(request.state, "role_id", "")) == "1":
        return None

    try:
        employee_rows = await table_api.get_table("VM_EmployeeMaster")
    except Exception as exc:
        print(f"[Runner360 hierarchy] Failed to load VM_EmployeeMaster: {exc}")
        raise HTTPException(
            status_code=502,
            detail="Unable to load VM_EmployeeMaster to filter the Runner360 runner list.",
        ) from exc

    return _resolve_descendant_employee_codes(employee_rows or [], employee_code)


async def _filter_runner_assignments_for_request(
    request: Request, assignments: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Apply hierarchy visibility to dropdown results and task creation."""
    visible_codes = await _visible_runner_codes_for_request(request)
    if visible_codes is None:
        return assignments, False
    return [
        assignment for assignment in assignments
        if _column_key(assignment.get("employee_code")) in visible_codes
    ], True


def _build_unique_pk(



    task_date: str,



    start_time: str,



    runner: str,



) -> str:



    """



    Build a unique application-originated business key.



    RowID remains the database identity. PK is only the unique business key



    required by the FMS table contract.



    """



    timestamp_part = datetime.now().strftime("%H%M%S%f")[:9]



    runner_part = "".join(



        ch for ch in runner.upper() if ch.isalnum()



    )[:12]



    value = f"R360-{task_date.replace('-', '')}-{start_time.replace(':', '')}-{runner_part}-{timestamp_part}"



    return value[:50]



def _build_planned_timestamps(
    task_start_datetime: str,
    lead_time_mins_raw: Any,
) -> tuple[str, str, int, str, str, str]:
    """Validate the start timestamp and calculate the expected return timestamp.

    Returns base timestamp, expected timestamp, lead minutes, task date,
    start time, and expected return time. All timestamps use local portal time.
    """
    try:
        start_dt = datetime.fromisoformat(task_start_datetime)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="I_TaskStartDateTime must be a valid local date and time.",
        ) from exc

    try:
        lead_time_mins = int(str(lead_time_mins_raw).strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="I_LeadTimeMins must be a whole number of minutes.",
        ) from exc

    if lead_time_mins < 1 or lead_time_mins > 10080:
        raise HTTPException(
            status_code=400,
            detail="I_LeadTimeMins must be between 1 and 10080 minutes.",
        )

    planned_dt = start_dt + timedelta(minutes=lead_time_mins)
    return (
        start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        planned_dt.strftime("%Y-%m-%d %H:%M:%S"),
        lead_time_mins,
        start_dt.strftime("%Y-%m-%d"),
        start_dt.strftime("%H:%M"),
        planned_dt.strftime("%H:%M"),
    )


async def _get_runner360_stage_config() -> dict[str, Any]:



    """



    Resolve Runner360 S1 configuration from the existing FMS_Config table.



    """



    config_rows = await metadata_service.get_fms_config(



        app_id=RUNNER360_APP_ID,



    )



    matching_rows = []



    for row in config_rows:



        if _normalise(



            row.get("FMS_NAME")



        ) != _normalise(RUNNER360_FMS_NAME):



            continue



        stage_no = _text(



            row.get("STAGE_NO")



            or row.get("StageNo")



            or row.get("STAGE")



        )



        if _normalise(stage_no) == _normalise(RUNNER360_STAGE_NO):



            matching_rows.append(row)



    if not matching_rows:



        raise HTTPException(



            status_code=500,



            detail=(



                "Runner360 S1 configuration was not found in FMS_Config "



                f"for App ID {RUNNER360_APP_ID}."



            ),



        )



    return matching_rows[0]





# ---------------------------------------------------------------------------
# Manager and Team Lead Runner360 planned time / lead-time editing
# ---------------------------------------------------------------------------

def _column_name_for(row: dict[str, Any], *names: str) -> str | None:
    wanted = {_column_key(name) for name in names}
    for key in row.keys():
        if _column_key(key) in wanted:
            return str(key)
    return None


def _is_blank_db_value(value: Any) -> bool:
    return value is None or str(value).strip().casefold() in {"", "none", "null"}


def _runner360_row_is_completed(row: dict[str, Any]) -> bool:
    actual = _lookup_case_insensitive(row, "S1_ALTimeStamp", "S1_ActualTimestamp", "S1_ActualTimeStamp")
    if not _is_blank_db_value(actual):
        return True
    status = _normalise(_lookup_case_insensitive(row, "S1_TaskStatus", "S1_Task_Status"))
    return status in {"completed", "complete", "done", "closed", "closedelay", "donedelay"}


def _runner360_row_is_cancelled(row: dict[str, Any]) -> bool:
    """Return True once a manager has revoked/cancelled this Runner360 task."""
    status = _normalise(_lookup_case_insensitive(row, "S1_TaskStatus", "S1_Task_Status"))
    return status in {"cancelled", "canceled", "revoked"}


def _local_guest_edit_enabled(request: Request) -> bool:
    """Allow local Guest test actions only when explicitly enabled on localhost in a non-production/test API environment."""
    enabled = os.getenv("RUNNER360_ALLOW_LOCAL_GUEST_EDIT", "").strip().casefold() in {"1", "true", "yes", "on"}
    if not enabled:
        return False

    environment = _normalise(getattr(settings, "environment", "development"))
    if environment not in {"development", "dev", "local", "test", "testing"}:
        return False

    db_target = _normalise(getattr(settings, "fms_db_target", ""))
    if db_target in {"prod", "production", "api 2", "api_2", "live"}:
        return False

    # Both ends must be loopback: Host alone can be spoofed.
    client_host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            return False
    except ValueError:
        return False

    return (request.url.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}


async def _get_current_employee_row(request: Request) -> dict[str, Any]:
    employee_code = _text(getattr(request.state, "employee_code", ""))
    if not employee_code:
        raise HTTPException(status_code=401, detail="Your authenticated EmployeeCode could not be resolved.")
    try:
        rows = await table_api.get_table("VM_EmployeeMaster", filters={"EmployeeCode": employee_code})
        matched = next((
            row for row in (rows or [])
            if _column_key(_lookup_case_insensitive(row, "EmployeeCode", "Employee Code")) == _column_key(employee_code)
        ), None)
        if matched is None:
            # Some gateways do not apply filters consistently; fall back to an exact in-memory match.
            all_rows = await table_api.get_table("VM_EmployeeMaster")
            matched = next((
                row for row in (all_rows or [])
                if _column_key(_lookup_case_insensitive(row, "EmployeeCode", "Employee Code")) == _column_key(employee_code)
            ), None)
    except Exception as exc:
        print(f"[Runner360 plan edit] Employee master lookup failed: {exc}")
        raise HTTPException(status_code=502, detail="Unable to validate Runner360 editing permissions against VM_EmployeeMaster.") from exc

    if not matched:
        raise HTTPException(status_code=401, detail="Your employee record was not found in VM_EmployeeMaster.")
    if not _is_active(_lookup_case_insensitive(matched, "Active", "ActiveStatus")):
        raise HTTPException(status_code=403, detail="Inactive employees cannot edit Runner360 planned details.")
    return matched


async def _get_runner360_row_by_task_id(task_id: str) -> tuple[dict[str, Any], str]:
    """Resolve the exact Runner360 data row and its physical RowID."""
    target = _text(task_id)
    candidates: list[dict[str, Any]] = []
    for filter_name in ("RowID", "PK"):
        try:
            filtered = await table_api.get_table(RUNNER360_TABLE, filters={filter_name: target})
            candidates.extend(row for row in (filtered or []) if isinstance(row, dict))
        except Exception as exc:
            print(f"[Runner360 plan edit] Lookup by {filter_name} failed for task {target}: {exc}")

        for row in candidates:
            row_id = _text(_lookup_case_insensitive(row, "RowID", "Row Id"))
            business_key = _text(_lookup_case_insensitive(row, "PK"))
            if row_id == target or business_key == target:
                if not row_id:
                    break
                return row, row_id
        candidates = []

    # Last-resort exact match if this API gateway ignores or mishandles filters.
    try:
        all_rows = await table_api.get_table(RUNNER360_TABLE)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Unable to load the Runner360 task row.") from exc
    for row in all_rows or []:
        if not isinstance(row, dict):
            continue
        row_id = _text(_lookup_case_insensitive(row, "RowID", "Row Id"))
        business_key = _text(_lookup_case_insensitive(row, "PK"))
        if row_id == target or business_key == target:
            if not row_id:
                raise HTTPException(status_code=500, detail="The matched Runner360 row has no RowID.")
            return row, row_id
    raise HTTPException(status_code=404, detail=f"Runner360 task '{target}' was not found.")



def _safe_float(value: Any) -> float | None:
    """Parse finite numeric coordinates without accepting NaN/Infinity."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_shift_clock(value: Any) -> clock_time | None:
    """Parse SQL/API shift values such as 09:00, 09:00:00, or datetime-like strings."""
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)
    if isinstance(value, clock_time):
        return value.replace(tzinfo=None)
    raw = _text(value)
    if not raw:
        return None

    candidates = [raw]
    # SQL/API adapters may serialize SQL TIME as a date-time string.
    if "T" in raw:
        candidates.append(raw.split("T", 1)[1].rstrip("Z"))
    if " " in raw and ":" in raw:
        candidates.append(raw.rsplit(" ", 1)[-1])

    formats = ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p")
    for candidate in candidates:
        candidate = candidate.strip()
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).time()
            except ValueError:
                continue
    return None


def _is_within_runner_shift(now: datetime, shift_start_value: Any, shift_end_value: Any) -> bool | None:
    """Return True inside the employee's shift, False outside, None when shift data is unusable.

    Lunch is part of the working location window: we deliberately do not subtract the
    employee's lunch interval from the shift window. Missing/invalid shift data returns
    None; callers must then enforce the branch geofence rather than assume after-hours.
    Supports ordinary and overnight shifts.
    """
    shift_start = _parse_shift_clock(shift_start_value)
    shift_end = _parse_shift_clock(shift_end_value)
    if shift_start is None or shift_end is None or shift_start == shift_end:
        return None

    current = now.time().replace(tzinfo=None)
    if shift_start < shift_end:
        return shift_start <= current <= shift_end
    # An overnight shift, e.g. 22:00 through 06:00.
    return current >= shift_start or current <= shift_end


def _gps_radius_meters() -> float:
    """Read the fallback radius if a legacy location source does not expose its own radius."""
    raw = os.getenv("RUNNER360_GPS_RADIUS_METERS", "500").strip()
    try:
        radius = float(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="RUNNER360_GPS_RADIUS_METERS must be a positive number.") from exc
    if not math.isfinite(radius) or radius <= 0 or radius > 10000:
        raise HTTPException(status_code=500, detail="RUNNER360_GPS_RADIUS_METERS must be greater than 0 and no more than 10000.")
    return radius


def _gps_max_accuracy_meters() -> float:
    """Maximum acceptable device-reported GPS uncertainty, separate from the branch geofence radius."""
    raw = os.getenv("RUNNER360_GPS_MAX_ACCURACY_METERS", "250").strip()
    try:
        maximum = float(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail="RUNNER360_GPS_MAX_ACCURACY_METERS must be a positive number.",
        ) from exc
    if not math.isfinite(maximum) or maximum <= 0 or maximum > 10000:
        raise HTTPException(
            status_code=500,
            detail="RUNNER360_GPS_MAX_ACCURACY_METERS must be greater than 0 and no more than 10000.",
        )
    return maximum


def _haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two GPS coordinates, in metres."""
    earth_radius_m = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return earth_radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


async def _resolve_runner360_branch_coordinates(branch_name: str) -> tuple[float, float, float, str] | None:
    """Resolve official branch coordinates from dbo.Z_Runner360_Location.

    This dedicated Runner360 location master stores BranchName, Latitude, Longitude,
    AllowedRadiusM and Active. Optional legacy sources are only fallback sources
    if the location row has not been configured; they are never hard-coded here.
    """
    configured_table = os.getenv("RUNNER360_BRANCH_LOCATION_TABLE", "").strip()
    tables_to_try: list[str] = ["Z_Runner360_Location"]
    for candidate in (configured_table, "VM_EmployeeMaster"):
        if candidate and all(_normalise(candidate) != _normalise(existing) for existing in tables_to_try):
            tables_to_try.append(candidate)

    branch_aliases = ("BranchName", "Branch", "BranchLocation", "LocationName", "OfficeName", "Location")
    latitude_aliases = ("Latitude", "BranchLatitude", "LocationLatitude", "GPSLatitude", "GeoLatitude", "BranchLat", "LocationLat", "GPSLat", "Lat")
    longitude_aliases = ("Longitude", "BranchLongitude", "LocationLongitude", "GPSLongitude", "GeoLongitude", "BranchLong", "LocationLong", "GPSLong", "Long", "Lng")

    for table_name in tables_to_try:
        try:
            rows = await table_api.get_table(table_name)
        except Exception as exc:
            print(f"[Runner360 IN] Could not read branch-location source '{table_name}': {exc}")
            continue

        for item in rows or []:
            if not isinstance(item, dict):
                continue
            master_branch = _text(_lookup_case_insensitive(item, *branch_aliases))
            if not master_branch or _branch_key(master_branch) != _branch_key(branch_name):
                continue

            # If an Active flag exists, inactive records must never verify an IN action.
            active_value = _lookup_case_insensitive(item, "Active", "IsActive", "ActiveStatus")
            if active_value is not None and not _is_active(active_value):
                continue

            latitude = _safe_float(_lookup_case_insensitive(item, *latitude_aliases))
            longitude = _safe_float(_lookup_case_insensitive(item, *longitude_aliases))
            if latitude is None or longitude is None:
                continue
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue

            radius_value = _safe_float(_lookup_case_insensitive(item, "AllowedRadiusM", "AllowedRadiusMeters", "RadiusMeters", "GeofenceRadiusM"))
            if _normalise(table_name) == _normalise("Z_Runner360_Location"):
                # The dedicated location master requires its own per-branch radius.
                if radius_value is None or radius_value <= 0 or radius_value > 10000:
                    print(f"[Runner360 IN] Invalid AllowedRadiusM for branch '{master_branch}' in Z_Runner360_Location")
                    continue
                radius_meters = radius_value
            else:
                radius_meters = radius_value if radius_value and 0 < radius_value <= 10000 else _gps_radius_meters()
            return latitude, longitude, radius_meters, table_name
    return None


async def _persist_runner360_gps(
    row: dict[str, Any], row_id: str, latitude: float, longitude: float,
    verified: bool, distance_meters: float | None,
) -> None:
    """Persist the captured location using the actual column names returned by Z_Runner360."""
    aliases = {
        "S1_RunnerLatitude": ("S1_RunnerLatitude", "S1_Latitude", "RunnerLatitude"),
        "S1_RunnerLongitude": ("S1_RunnerLongitude", "S1_Longitude", "RunnerLongitude"),
        "S1_LocationVerified": ("S1_LocationVerified", "LocationVerified"),
        "S1_LocationDistanceM": ("S1_LocationDistanceM", "LocationDistanceM", "S1_DistanceMeters"),
    }
    columns = {logical: _column_name_for(row, *names) for logical, names in aliases.items()}
    missing = [name for name, column in columns.items() if not column]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=(
                "Z_Runner360 is missing GPS storage columns: " + ", ".join(missing)
                + ". Add the missing columns before using Runner360 IN."
            ),
        )

    payload = {
        columns["S1_RunnerLatitude"]: round(latitude, 7),
        columns["S1_RunnerLongitude"]: round(longitude, 7),
        columns["S1_LocationVerified"]: "Y" if verified else "N",
        columns["S1_LocationDistanceM"]: round(distance_meters, 1) if distance_meters is not None else None,
    }
    try:
        result = await api_client.post(
            "update_row",
            query_params={"action": "update", "table": RUNNER360_TABLE},
            json_body={
                "action": "update",
                "table": RUNNER360_TABLE,
                "data": payload,
                "where": {"RowID": row_id},
            },
        )
    except Exception as exc:
        print(f"[Runner360 IN] Failed saving GPS for RowID {row_id}: {exc}")
        raise HTTPException(status_code=502, detail="GPS was captured but could not be saved to Z_Runner360.") from exc

    if isinstance(result, dict):
        result_status = _normalise(result.get("status"))
        if result.get("success") is False or result_status in {"error", "failed", "failure"} or result.get("error"):
            detail = _text(result.get("detail") or result.get("error") or result.get("message"))
            raise HTTPException(status_code=502, detail="Unable to save Runner360 GPS data" + (f": {detail}" if detail else "."))


async def _persist_runner360_busy_time(
    row: dict[str, Any], row_id: str, actual_datetime: datetime,
) -> tuple[int | None, bool]:
    """Store actual task duration in Z_Runner360.BusyTimeMins after successful IN.

    Busy time is the elapsed whole minutes from the planned/base task start
    (S1_PLBaseTS) to the actual IN timestamp (S1_ALTimeStamp). A failure to
    update this dashboard metric must not reverse a successful IN/SP operation.
    """
    start_value = _lookup_case_insensitive(
        row, "S1_PLBaseTS", "S1_BaseTS", "PLBaseTS", "I_TaskDateTime", "I_TaskStartDateTime"
    )
    start_datetime = parse_datetime(start_value)
    if start_datetime is None:
        print(f"[Runner360 IN] BusyTimeMins not saved: no valid planned/base start time for RowID {row_id}.")
        return None, False

    busy_minutes = max(0, int((actual_datetime - start_datetime).total_seconds() // 60))
    payload = {
        "action": "update",
        "table": RUNNER360_TABLE,
        "data": {"BusyTimeMins": busy_minutes},
        "where": {"RowID": row_id},
    }
    try:
        result = await api_client.post(
            "update_row",
            query_params={"action": "update", "table": RUNNER360_TABLE},
            json_body=payload,
        )
        if isinstance(result, dict):
            status = _normalise(result.get("status"))
            if result.get("success") is False or status in {"error", "failed", "failure"} or result.get("error"):
                detail = _text(result.get("detail") or result.get("error") or result.get("message"))
                print(f"[Runner360 IN] BusyTimeMins update failed for RowID {row_id}: {detail or result!r}")
                return busy_minutes, False
        print(f"[Runner360 IN] Saved BusyTimeMins={busy_minutes} for RowID {row_id}.")
        return busy_minutes, True
    except Exception as exc:
        print(f"[Runner360 IN] BusyTimeMins update failed for RowID {row_id}: {exc}")
        return busy_minutes, False


async def _runner360_mark_in_impl(request: Request, task_id: str, token_employee_code: str = ""):
    """Shared GPS/geofence/status implementation for authenticated and signed-link check-ins."""
    employee_code = _text(token_employee_code or getattr(request.state, "employee_code", ""))
    local_guest_test = _local_guest_edit_enabled(request) and not employee_code
    employee = None if local_guest_test else await _get_current_employee_row(request)

    row, row_id = await _get_runner360_row_by_task_id(task_id)
    if _runner360_row_is_cancelled(row):
        raise HTTPException(status_code=409, detail="This Runner360 task is Cancelled; IN is disabled.")
    if _runner360_row_is_completed(row):
        raise HTTPException(status_code=409, detail="This Runner360 task is already completed.")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid GPS request body.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="GPS request must be a JSON object.")

    latitude = _safe_float(body.get("latitude"))
    longitude = _safe_float(body.get("longitude"))
    accuracy = _safe_float(body.get("accuracy"))
    if latitude is None or longitude is None or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="The browser did not provide valid GPS coordinates. Allow location access and try again.")
    if accuracy is None or accuracy < 0:
        raise HTTPException(status_code=400, detail="GPS accuracy could not be resolved. Please retry with location access enabled.")

    branch_name = _text(_lookup_case_insensitive(row, "I_Branch", "Branch"))
    runner_name = _text(_lookup_case_insensitive(row, "I_Runner", "S1_Doer", "Doer"))
    if not branch_name or not runner_name:
        raise HTTPException(status_code=500, detail="Runner360 task is missing the assigned branch or runner.")

    # Resolve the runner's active EmployeeCode from the same master views used during task creation.
    assignments = await _get_runner_assignments()
    matched_runner = next((
        item for item in assignments
        if _normalise(item.get("employee_name")) == _normalise(runner_name)
        and _branch_key(item.get("branch")) == _branch_key(branch_name)
    ), None)
    if not matched_runner or not _text(matched_runner.get("employee_code")):
        raise HTTPException(
            status_code=409,
            detail="The assigned runner could not be resolved to an active Runner-team EmployeeCode in VM_Team/VM_EmployeeMaster.",
        )
    doer_code = _text(matched_runner.get("employee_code"))

    # Non-Guest callers must be the assigned runner or an authorized manager/admin.
    if not local_guest_test:
        current_code = _column_key(employee_code)
        role_id = _text(getattr(request.state, "role_id", ""))
        manager_or_admin = role_id == "1" or _is_active(_lookup_case_insensitive(employee or {}, "IsManager", "Is Manager"))
        if current_code != _column_key(doer_code) and not manager_or_admin:
            raise HTTPException(status_code=403, detail="Only the assigned runner, an active manager, or a Role ID 1 administrator can press IN.")
    else:
        print(f"[Runner360 LOCAL GUEST TEST] IN requested for task {task_id} from {request.client.host if request.client else 'unknown host'}")

    # Use one server-side timestamp for both the shift decision and the FMS actual timestamp.
    checkin_now = datetime.now()
    inside_shift = _is_within_runner_shift(
        checkin_now,
        matched_runner.get("shift_start_time"),
        matched_runner.get("shift_end_time"),
    )
    outside_shift_hours = inside_shift is False
    shift_start_text = _text(matched_runner.get("shift_start_time"))
    shift_end_text = _text(matched_runner.get("shift_end_time"))

    branch_location = await _resolve_runner360_branch_coordinates(branch_name)
    distance_meters: float | None = None
    radius_meters = _gps_radius_meters()
    location_source = "unavailable"
    location_verified = False

    if branch_location is not None:
        branch_latitude, branch_longitude, radius_meters, location_source = branch_location
        distance_meters = _haversine_distance_meters(latitude, longitude, branch_latitude, branch_longitude)

    # GPS uncertainty is quality metadata, separate from distance to the branch.
    max_accuracy_meters = _gps_max_accuracy_meters()
    if accuracy > max_accuracy_meters:
        await _persist_runner360_gps(row, row_id, latitude, longitude, False, distance_meters)
        raise HTTPException(
            status_code=409,
            detail=(
                f"GPS accuracy is about {accuracy:.0f} m, exceeding the configured maximum of "
                f"{max_accuracy_meters:.0f} m. Enable precise device location or retry on a phone outdoors. "
                "IN has not been recorded."
            ),
        )

    # During the scheduled shift, including the lunch interval, the runner must be
    # within the configured branch radius. Lunch is not treated as after-hours.
    # If shift values are missing or invalid, fail closed and keep the geofence active.
    enforce_work_location = not outside_shift_hours

    if branch_location is None and enforce_work_location:
        await _persist_runner360_gps(row, row_id, latitude, longitude, False, None)
        missing_shift_note = " Shift times are unavailable, so the normal branch-location rule remains active." if inside_shift is None else ""
        raise HTTPException(
            status_code=503,
            detail=(
                f"GPS was captured, but IN was not recorded because coordinates for '{branch_name}' "
                "could not be found in dbo.Z_Runner360_Location."
                + missing_shift_note
            ),
        )

    if branch_location is not None and distance_meters is not None:
        if enforce_work_location and distance_meters > radius_meters:
            await _persist_runner360_gps(row, row_id, latitude, longitude, False, distance_meters)
            shift_note = (
                f" Runner shift is {shift_start_text or 'unknown'}–{shift_end_text or 'unknown'}; "
                "lunch remains part of working hours."
                if inside_shift is True else
                " Shift hours are missing or invalid, so the branch check is enforced by default."
                if inside_shift is None else ""
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"GPS shows you are approximately {distance_meters:.0f} m from the assigned branch; "
                    f"the allowed radius is {radius_meters:.0f} m. IN is allowed away from the branch only "
                    "outside the runner's scheduled shift."
                    + shift_note
                ),
            )
        location_verified = distance_meters <= radius_meters
    elif outside_shift_hours:
        # After the scheduled shift, GPS capture and IN are allowed even if the
        # branch master entry is unavailable. Save coordinates without claiming
        # they were verified against the assigned branch.
        location_verified = False

    # Save the actual GPS every time. N means it was not verified inside the branch
    # radius; after-hours IN may still proceed, preserving coordinates and distance.
    await _persist_runner360_gps(row, row_id, latitude, longitude, location_verified, distance_meters)

    if outside_shift_hours and not location_verified:
        print(
            f"[Runner360 IN] After-shift check-in outside verified branch radius; "
            f"RowID={row_id}; shift={shift_start_text}-{shift_end_text}; distance_m={distance_meters}; "
            f"lat={latitude}; lon={longitude}; source={location_source}"
        )

    planned_timestamp = _text(_lookup_case_insensitive(row, "S1_PLTimeStamp", "S1_PlannedTimestamp"))
    if not planned_timestamp:
        raise HTTPException(status_code=500, detail="S1_PLTimeStamp is empty; Runner360 IN cannot calculate task status.")
    try:
        lead_minutes = int(float(str(_lookup_case_insensitive(row, "S1_LeadTimeMins", "S1_LeadTimeMins" )).strip()))
    except (TypeError, ValueError):
        raise HTTPException(status_code=500, detail="S1_LeadTimeMins is missing or invalid.")
    if lead_minutes < 1:
        raise HTTPException(status_code=500, detail="S1_LeadTimeMins must be greater than zero.")

    actual_timestamp = checkin_now.strftime("%Y-%m-%d %H:%M:%S")
    kpi_raw = _normalise(_lookup_case_insensitive(row, "S1_KPIApplicable", "S1_KPIApplicableFlag"))
    kpi_applicable = "Y" if kpi_raw in {"y", "yes", "1", "true"} else "N"
    proc_payload = {
        "PLTimeStamp": planned_timestamp,
        "ALTimeStamp": actual_timestamp,
        "DoerCode": doer_code,
        "RowID": int(row_id),
        "AppID": RUNNER360_APP_ID,
        "StageNo": RUNNER360_STAGE_NO,
        "LeadTimeMins": lead_minutes,
        "KPIApplicable": kpi_applicable,
        "ALHelper": _text(_lookup_case_insensitive(row, "S1_ALHelper")) or "FMS",
    }
    try:
        sp_result = await stage_service.run_named_stage_procedure(
            str(row_id), RUNNER360_STAGE_NO, "SP_FMS_ALTimeStampStatus", proc_payload
        )
    except Exception as exc:
        print(f"[Runner360 IN] SP_FMS_ALTimeStampStatus failed for RowID {row_id}: {exc}")
        raise HTTPException(
            status_code=502,
            detail="GPS verified and saved, but actual timestamp/status could not be saved by SP_FMS_ALTimeStampStatus. You can retry IN after checking the server log.",
        ) from exc

    if isinstance(sp_result, dict):
        sp_status = _normalise(sp_result.get("status"))
        if sp_result.get("success") is False or sp_status in {"error", "failed", "failure"}:
            detail = _text(sp_result.get("detail") or sp_result.get("message") or sp_result.get("error"))
            raise HTTPException(
                status_code=502,
                detail="GPS verified and saved, but SP_FMS_ALTimeStampStatus reported a failure" + (f": {detail}" if detail else "."),
            )

    # Busy-time storage is a dashboard metric, written after the standard IN
    # procedure succeeds. It is best-effort so a metric write failure does not
    # cause the runner to retry an IN that has already been recorded.
    busy_time_minutes, busy_time_saved = await _persist_runner360_busy_time(row, row_id, checkin_now)

    if location_verified and distance_meters is not None:
        user_message = f"IN recorded successfully. GPS verified within {distance_meters:.0f} m of {branch_name}."
        location_status = "VerifiedAtAssignedBranch"
    elif outside_shift_hours and distance_meters is not None:
        user_message = (
            f"IN recorded outside scheduled shift hours. GPS coordinates were saved; the reported location "
            f"is approximately {distance_meters:.0f} m from {branch_name} and is marked outside the working location."
        )
        location_status = "OutsideWorkingLocation"
    elif outside_shift_hours:
        user_message = (
            "IN recorded outside scheduled shift hours. GPS coordinates were saved, but the assigned branch "
            "distance could not be calculated because its coordinates are not configured."
        )
        location_status = "BranchCoordinatesUnavailable"
    else:
        # This path should normally be covered by the working-location checks above.
        user_message = "IN recorded; location verification status is unavailable."
        location_status = "Unverified"

    print(
        f"[Runner360 IN] Completed RowID={row_id}; branch='{branch_name}'; "
        f"distance_m={distance_meters}; radius_m={radius_meters}; inside_shift={inside_shift}; "
        f"location_verified={location_verified}; location_status='{location_status}'; "
        f"location_source='{location_source}'; actual='{actual_timestamp}'; sp_result={sp_result!r}"
    )
    return JSONResponse({
        "ok": True,
        "message": user_message,
        "task_id": str(row_id),
        "actual_timestamp": actual_timestamp,
        "distance_meters": round(distance_meters, 1) if distance_meters is not None else None,
        "radius_meters": radius_meters,
        "location_verified": location_verified,
        "location_status": location_status,
        "outside_working_hours": outside_shift_hours,
        "shift_start_time": shift_start_text,
        "shift_end_time": shift_end_text,
        "task_status": _text(sp_result.get("TaskStatus") or sp_result.get("task_status")) if isinstance(sp_result, dict) else "Recorded",
        "busy_time_minutes": busy_time_minutes,
        "busy_time_saved": busy_time_saved,
    }, headers={"Cache-Control": "no-store"})


@router.post("/forms/runner360/{task_id}/in", response_class=JSONResponse)
async def runner360_mark_in(request: Request, task_id: str):
    """Authenticated in-portal check-in (existing route)."""
    return await _runner360_mark_in_impl(request, task_id)


async def _validate_mobile_checkin_task(token: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    claims = _read_mobile_checkin_token(token)
    task_id = str(claims["task_id"]).strip()
    employee_code = str(claims["employee_code"]).strip()
    row, row_id = await _get_runner360_row_by_task_id(task_id)
    if _runner360_row_is_cancelled(row):
        raise HTTPException(status_code=409, detail="This Runner360 task is Cancelled; Check In is disabled.")
    if _runner360_row_is_completed(row):
        raise HTTPException(status_code=409, detail="This Runner360 task is already completed.")
    runner_name = _text(_lookup_case_insensitive(row, "I_Runner", "S1_Doer", "Doer"))
    branch_name = _text(_lookup_case_insensitive(row, "I_Branch", "Branch"))
    assignments = await _get_runner_assignments()
    matched = next((
        item for item in assignments
        if _normalise(item.get("employee_name")) == _normalise(runner_name)
        and _branch_key(item.get("branch")) == _branch_key(branch_name)
        and _column_key(item.get("employee_code")) == _column_key(employee_code)
    ), None)
    if not matched:
        raise HTTPException(status_code=403, detail="This Check In link does not match the runner assigned to this task.")
    return row, row_id, claims


@router.get("/runner360/mobile/{token}", response_class=HTMLResponse)
async def runner360_mobile_checkin_page(request: Request, token: str):
    """Public, signed, task-specific mobile check-in page for WhatsApp links."""
    row, row_id, _claims = await _validate_mobile_checkin_task(token)
    return templates.TemplateResponse(request, "custom_forms/runner360_mobile_checkin.html", {
        "task_id": str(row_id),
        "runner": _text(_lookup_case_insensitive(row, "I_Runner", "S1_Doer", "Doer")) or "Runner",
        "branch": _text(_lookup_case_insensitive(row, "I_Branch", "Branch")) or "-",
        "purpose": _text(_lookup_case_insensitive(row, "I_Purpose", "Purpose")) or "-",
        "start": _text(_lookup_case_insensitive(row, "I_TaskStartDateTime", "S1_PLTimeStamp", "PlannedStart")) or "-",
        "token": token,
    }, headers={"Cache-Control": "no-store"})


@router.post("/runner360/mobile/{token}/in", response_class=JSONResponse)
async def runner360_mobile_mark_in(request: Request, token: str):
    """Token-gated mobile endpoint; only permits the employee named in the signed link."""
    _row, _row_id, claims = await _validate_mobile_checkin_task(token)
    employee_code = str(claims["employee_code"]).strip()
    request.state.employee_code = employee_code
    request.state.role_id = ""
    return await _runner360_mark_in_impl(request, str(claims["task_id"]), token_employee_code=employee_code)


def _is_team_lead_employee(employee: dict[str, Any]) -> bool:
    """Identify Team Leads from current employee-master attributes, not usernames."""
    grade = _normalise(_lookup_case_insensitive(employee, "Grade", "EmployeeGrade"))
    if grade in {"tl", "team lead", "team leader", "teamlead", "teamleader"}:
        return True

    title_values = (
        _lookup_case_insensitive(employee, "Position"),
        _lookup_case_insensitive(employee, "JobTitle", "Job Title"),
        _lookup_case_insensitive(employee, "Designation"),
    )
    for raw_title in title_values:
        title = _normalise(raw_title)
        if "team lead" in title or "team leader" in title:
            return True
        if title.startswith(("tl-", "tl ", "tl/")):
            return True
    return False


def _can_edit_runner360_planned_details(
    employee: dict[str, Any],
    row: dict[str, Any],
    role_id: Any = None,
) -> tuple[bool, bool, bool]:
    """Only an active manager may edit planned start time and lead time."""
    # Authorization comes from the server-resolved active employee record.
    # A browser role, Role ID 1, Team Lead designation, or branch match alone
    # does not grant access to change these planned values.
    is_manager = _is_active(_lookup_case_insensitive(employee, "IsManager", "Is Manager"))
    is_team_lead = _is_team_lead_employee(employee)
    return is_manager, is_manager, is_team_lead


@router.get("/forms/runner360/{task_id}/plan-edit-permission", response_class=JSONResponse)
async def runner360_plan_edit_permission(request: Request, task_id: str):
    """Expose the planned-details button to viewers, but authorize editing for managers only."""
    row, _row_id = await _get_runner360_row_by_task_id(task_id)
    completed = _runner360_row_is_completed(row)
    cancelled = _runner360_row_is_cancelled(row)
    show_edit_button = not cancelled
    employee_code = _text(getattr(request.state, "employee_code", ""))

    # Guest/anonymous viewers can see the action, but it is returned disabled.
    # Local guest test mode may still be used for the separate revoke test flow;
    # it never grants planned-details edit permission.
    if not employee_code:
        local_guest_test = _local_guest_edit_enabled(request)
        return JSONResponse({
            "ok": True,
            "is_manager": False,
            "is_team_lead": False,
            "is_local_guest": True,
            "completed": completed,
            "cancelled": cancelled,
            "show_edit_button": show_edit_button,
            "can_edit": False,
            "can_revoke": bool(local_guest_test and not completed and not cancelled),
            "edit_label": "Edit Planned Details" if show_edit_button else "",
        }, headers={"Cache-Control": "no-store"})

    employee = await _get_current_employee_row(request)
    role_id = getattr(request.state, "role_id", None)
    has_role_permission, _manager_flag, is_team_lead = _can_edit_runner360_planned_details(employee, row, role_id)
    is_manager = _is_active(_lookup_case_insensitive(employee, "IsManager", "Is Manager"))
    can_edit = bool(has_role_permission and not cancelled)
    # Keep the existing Role ID 1 administrator allowance for revoking tasks only.
    is_role1_admin = _text(role_id) == "1"
    can_revoke = bool((is_manager or is_role1_admin) and not completed and not cancelled)
    return JSONResponse({
        "ok": True,
        "is_manager": is_manager,
        "is_team_lead": is_team_lead,
        "is_local_guest": False,
        "completed": completed,
        "cancelled": cancelled,
        "show_edit_button": show_edit_button,
        "can_edit": can_edit,
        "can_revoke": can_revoke,
        "edit_label": "Edit Planned Details" if show_edit_button else "",
    }, headers={"Cache-Control": "no-store"})


@router.post("/forms/runner360/{task_id}/planned-details", response_class=JSONResponse)
async def runner360_update_planned_details(request: Request, task_id: str):
    """Persist planned time; only an active manager can update it, including after S1 completion."""
    employee = await _get_current_employee_row(request)
    row, row_id = await _get_runner360_row_by_task_id(task_id)

    role_id = getattr(request.state, "role_id", None)
    has_role_permission, _manager_flag, _is_team_lead = _can_edit_runner360_planned_details(employee, row, role_id)
    if not has_role_permission:
        raise HTTPException(
            status_code=403,
            detail="Only an active manager can edit Runner360 Task Start Date & Time and Lead Time.",
        )

    if _runner360_row_is_cancelled(row):
        raise HTTPException(status_code=409, detail="Runner360 task is Cancelled; planned details can no longer be changed.")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    start_input = _text(body.get("I_TaskStartDateTime"))
    lead_input = body.get("I_LeadTimeMins")
    base_ts, planned_ts, lead_mins, task_date, start_time, expected_time = _build_planned_timestamps(start_input, lead_input)

    # Build updates only for columns actually present in this row, allowing the
    # current combined timestamp schema and earlier split date/time rows to coexist.
    # _column_key() returns lowercase alphanumeric keys, so keep this map's
    # keys normalized the same way. Mixed-case keys here would never match.
    update_values: dict[str, Any] = {
        "itaskstartdatetime": base_ts,
        "itaskdate": task_date,
        "itaskstarttime": start_time,
        "ileadtimemins": lead_mins,
        "iexpectedreturntime": expected_time,
        "iexpectedreturndatetime": planned_ts,
        "s1plbasets": base_ts,
        "s1leadtimemins": lead_mins,
        "s1pltimestamp": planned_ts,
    }

    payload: dict[str, Any] = {}
    for existing_column in row.keys():
        normalized_column = _column_key(existing_column)
        if normalized_column not in update_values:
            continue
        value = update_values[normalized_column]
        old_value = _lookup_case_insensitive(row, existing_column)
        if normalized_column in {"itaskstarttime", "iexpectedreturntime"}:
            # Legacy rows may store either a time-only value or a full timestamp.
            old_text = _text(old_value)
            has_date_part = len(old_text) >= 10 and "-" in old_text
            if normalized_column == "itaskstarttime" and has_date_part:
                value = base_ts
            elif normalized_column == "iexpectedreturntime" and has_date_part:
                value = planned_ts
        payload[str(existing_column)] = value

    if not any(_column_key(name) in {_column_key(key) for key in row.keys()} for name in ("I_TaskStartDateTime", "I_TaskStartTime")):
        raise HTTPException(status_code=500, detail="Z_Runner360 has no supported task-start column.")
    if not any(_column_key(name) in {_column_key(key) for key in row.keys()} for name in ("I_LeadTimeMins", "S1_LeadTimeMins")):
        raise HTTPException(status_code=500, detail="Z_Runner360 has no supported lead-time column.")
    if not payload:
        raise HTTPException(status_code=500, detail="No editable Runner360 planned-time columns were found.")

    # Apply standard update audit fields for the authenticated manager.
    employee_code = _text(getattr(request.state, "employee_code", ""))
    audit_values = {
        "updatedbyemployeecode": employee_code,
        "updatedby": employee_code,
        "modifiedby": employee_code,
        "modifiedbyemployeecode": employee_code,
        "updateddatetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updateddate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "modifiedon": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "iupdatedby": employee_code,
        "iupdatedts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    for existing_column in row.keys():
        norm = _column_key(existing_column)
        if norm in audit_values:
            payload[str(existing_column)] = audit_values[norm]

    try:
        result = await api_client.post(
            "update_row",
            query_params={"action": "update", "table": RUNNER360_TABLE},
            json_body={
                "action": "update",
                "table": RUNNER360_TABLE,
                "data": payload,
                "where": {"RowID": row_id},
            },
        )
    except Exception as exc:
        print(f"[Runner360 plan edit] Save failed for RowID {row_id}: {exc}")
        raise HTTPException(status_code=502, detail="Unable to save the updated Runner360 planned details.") from exc

    if isinstance(result, dict):
        error_text = _text(result.get("error") or result.get("message") if result.get("success") is False else result.get("error"))
        if error_text:
            raise HTTPException(status_code=502, detail=f"Runner360 update failed: {error_text}")

    return JSONResponse({
        "ok": True,
        "message": "Runner360 planned details updated successfully.",
        "task_start_datetime": base_ts,
        "lead_time_minutes": lead_mins,
        "expected_return_datetime": planned_ts,
    })


@router.post("/forms/runner360/{task_id}/revoke", response_class=JSONResponse)
async def runner360_revoke_task(request: Request, task_id: str):
    """Allow managers/admins to cancel tasks; local Guest tests are explicitly opt-in and localhost-only."""
    employee_code = _text(getattr(request.state, "employee_code", ""))
    local_guest_test = _local_guest_edit_enabled(request) and not employee_code
    if not local_guest_test:
        employee = await _get_current_employee_row(request)
        role_id = getattr(request.state, "role_id", None)
        is_role1_admin = _text(role_id) == "1"
        is_manager = _is_active(_lookup_case_insensitive(employee, "IsManager", "Is Manager"))
        if not (is_role1_admin or is_manager):
            raise HTTPException(status_code=403, detail="Only an active manager or Role ID 1 administrator can revoke a Runner360 task.")
    else:
        print(f"[Runner360 LOCAL GUEST TEST] Guest revoke requested for task {task_id} from {request.client.host if request.client else 'unknown host'}")

    row, row_id = await _get_runner360_row_by_task_id(task_id)
    if _runner360_row_is_cancelled(row):
        return JSONResponse({"ok": True, "status": "Cancelled", "message": "This Runner360 task is already Cancelled."})
    if _runner360_row_is_completed(row):
        raise HTTPException(status_code=409, detail="A completed Runner360 task cannot be cancelled.")

    status_column = _column_name_for(row, "S1_TaskStatus", "S1_Task_Status")
    if not status_column:
        raise HTTPException(status_code=500, detail="Z_Runner360 does not expose an S1 task-status column; cancellation was not saved.")

    payload: dict[str, Any] = {status_column: "Cancelled"}
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not local_guest_test:
        audit_values = {
            "updatedbyemployeecode": employee_code,
            "updatedby": employee_code,
            "modifiedby": employee_code,
            "modifiedbyemployeecode": employee_code,
            "updateddatetime": now_text,
            "updateddate": now_text,
            "modifiedon": now_text,
            "iupdatedby": employee_code,
            "iupdatedts": now_text,
        }
        for existing_column in row.keys():
            normalized = _column_key(existing_column)
            if normalized in audit_values:
                payload[str(existing_column)] = audit_values[normalized]

    try:
        result = await api_client.post(
            "update_row",
            query_params={"action": "update", "table": RUNNER360_TABLE},
            json_body={
                "action": "update",
                "table": RUNNER360_TABLE,
                "data": payload,
                "where": {"RowID": row_id},
            },
        )
    except Exception as exc:
        print(f"[Runner360 revoke] Save failed for RowID {row_id}: {exc}")
        raise HTTPException(status_code=502, detail="Unable to cancel this Runner360 task.") from exc

    if isinstance(result, dict) and result.get("success") is False:
        detail = _text(result.get("error") or result.get("message")) or "The database rejected the cancellation."
        raise HTTPException(status_code=502, detail=f"Runner360 cancellation failed: {detail}")
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=502, detail=f"Runner360 cancellation failed: {_text(result.get('error'))}")

    try:
        dashboard_service.invalidate_dashboard_table_cache(RUNNER360_TABLE)
    except Exception as cache_exc:
        # Cancellation has already been saved; cache invalidation is best-effort.
        print(f"[Runner360 revoke] Dashboard cache invalidation warning: {cache_exc}")

    print(f"[Runner360 revoke] RowID {row_id} marked Cancelled by {'local Guest test mode' if local_guest_test else 'EmployeeCode ' + employee_code}.")
    return JSONResponse({
        "ok": True,
        "status": "Cancelled",
        "message": "Runner360 task Cancelled successfully.",
        "task_id": str(row_id),
    })


@router.get("/forms/runner360/holidays", response_class=JSONResponse)
async def runner360_holidays():
    """Return company holidays to the Runner360 custom calendar."""
    holidays = await _get_runner360_holiday_ranges()
    return JSONResponse(
        content={"ok": True, "server_date": date.today().isoformat(), "holidays": holidays},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/forms/runner360/options", response_class=JSONResponse)
async def runner360_assignment_options(request: Request):
    # Return active Runner-team members grouped by branch. Authenticated users see
    # only runner descendants in their own company-wide reporting hierarchy.
    assignments = await _get_runner_assignments()
    assignments, hierarchy_filtered = await _filter_runner_assignments_for_request(request, assignments)
    await _get_runner_busy_status(assignments)

    branches = sorted(
        {item["branch"] for item in assignments if item.get("branch")},
        key=str.casefold,
    )

    return JSONResponse(
        content={
            "ok": True,
            "branches": branches,
            "runners": assignments,
            "hierarchy_filtered": hierarchy_filtered,
            "message": (
                "Runner list is limited to your reporting hierarchy."
                if hierarchy_filtered else ""
            ),
        },
        headers={"Cache-Control": "no-store"},
    )





@router.get(

    "/forms/runner360/input",

    response_class=HTMLResponse,

)

async def runner360_input_form(request: Request):



    """



    Render the Runner360 Team Lead Input Form.



    """



    return templates.TemplateResponse(



        request,



        "custom_forms/Z_Runner360.html",



        {



            "runner360_default_date": date.today().isoformat(),



        },



    )



@router.post(



    "/forms/runner360/input",



)



async def runner360_create_task(request: Request):



    """



    Create a new Runner360 task from the Team Lead Input Form.



    Request body:



        {



            "I_Branch": "...",



            "I_Runner": "...",



            "I_TaskStartDateTime": "YYYY-MM-DDTHH:MM",



            "I_LeadTimeMins": 120,



            "I_Purpose": "...",



            "I_ExpectedReturnTime": "calculated HH:MM",



        }



    The new record is inserted into Z_Runner360.



    """



    # ------------------------------------------------------------



    # Read JSON request



    # ------------------------------------------------------------



    try:



        payload = await request.json()



    except Exception as exc:



        raise HTTPException(



            status_code=400,



            detail="Invalid JSON request.",



        ) from exc



    if not isinstance(payload, dict):



        raise HTTPException(



            status_code=400,



            detail="Runner360 submission must be a JSON object.",



        )



    # ------------------------------------------------------------



    # Read Team Lead inputs



    # ------------------------------------------------------------



    branch = _text(payload.get("I_Branch"))



    runner = _text(payload.get("I_Runner"))



    task_start_datetime = _text(payload.get("I_TaskStartDateTime"))
    lead_time_input = payload.get("I_LeadTimeMins")

    # Backward compatibility for older form clients: derive a datetime from
    # I_TaskDate + I_TaskStartTime when the new combined field is absent.
    if not task_start_datetime:
        legacy_date = _text(payload.get("I_TaskDate"))
        legacy_time = _text(payload.get("I_TaskStartTime"))
        if legacy_date and legacy_time:
            task_start_datetime = f"{legacy_date}T{legacy_time}"

    purpose = _text(payload.get("I_Purpose"))
    expected_return_time = ""
    task_date = ""
    task_start_time = ""



    required_fields = {



        "I_Branch": branch,



        "I_Runner": runner,



        "I_TaskStartDateTime": task_start_datetime,



        "I_LeadTimeMins": _text(lead_time_input),



        "I_Purpose": purpose,



    }



    missing = [



        field



        for field, value in required_fields.items()



        if not value



    ]



    if missing:



        raise HTTPException(



            status_code=400,



            detail=(



                "The following required fields are missing: "



                + ", ".join(missing)



            ),



        )



    # ------------------------------------------------------------



        # ------------------------------------------------------------

    # Validate Branch + Runner against the current master views.

    # The browser submits employee code for stable selection; name-only

    # callers remain supported for compatibility with existing form clients.

    # ------------------------------------------------------------



    runner_employee_code = _text(payload.get("I_RunnerEmployeeCode"))

    assignments = await _get_runner_assignments()
    # Apply the same reporting-hierarchy scope on the server; a hidden
    # runner must not be assignable by manually modifying the request payload.
    assignments, _hierarchy_filtered = await _filter_runner_assignments_for_request(request, assignments)
    # Re-check live availability on the server; disabled HTML options are not security.
    await _get_runner_busy_status(assignments)

    selected_assignment = None



    for assignment in assignments:

        branch_matches = _branch_key(assignment.get("branch")) == _branch_key(branch)

        code_matches = (

            runner_employee_code

            and _column_key(assignment.get("employee_code")) == _column_key(runner_employee_code)

        )

        name_matches = _normalise(assignment.get("employee_name")) == _normalise(runner)

        if branch_matches and (code_matches or (not runner_employee_code and name_matches)):

            selected_assignment = assignment

            break



    if not selected_assignment:

        raise HTTPException(

            status_code=400,

            detail=(

                "The selected runner is not an active member of the Runner team "

                "for the selected branch. Refresh the form and select again."

            ),

        )

    if selected_assignment.get("is_busy"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{selected_assignment['employee_name']} is Busy: Z_KPI_MasterRecord has a planned "
                "timestamp without an actual timestamp. Select another runner or refresh the form."
            ),
        )


    # Store canonical values from the master views, never values trusted from the browser.

    branch = selected_assignment["branch"]

    runner = selected_assignment["employee_name"]



# Validate date/time and build S1 planned values



    # ------------------------------------------------------------



    try:



        (
            planned_base_ts,
            planned_timestamp,
            lead_time_mins,
            task_date,
            task_start_time,
            expected_return_time,
        ) = _build_planned_timestamps(
            task_start_datetime,
            lead_time_input,
        )



    except HTTPException:



        raise



    except Exception as exc:



        raise HTTPException(



            status_code=400,



            detail=f"Invalid Runner360 date/time values: {exc}",



        ) from exc

    await _validate_runner360_working_date(task_date)



    # ------------------------------------------------------------



    # Validate FMS configuration exists



    # ------------------------------------------------------------



    stage_config = await _get_runner360_stage_config()



    config_app_id = _text(



        stage_config.get("APP_ID")



        or stage_config.get("app_id")



    )



    config_table = _text(



        stage_config.get("TABLE_NAME")



        or stage_config.get("table_name")



    )



    if config_app_id and config_app_id != RUNNER360_APP_ID:



        raise HTTPException(



            status_code=500,



            detail=(



                "Runner360 App ID mismatch between code and FMS_Config."



            ),



        )



    if config_table and _normalise(config_table) != _normalise(RUNNER360_TABLE):



        raise HTTPException(



            status_code=500,



            detail=(



                "Runner360 table mismatch between code and FMS_Config."



            ),



        )



    # ------------------------------------------------------------



    # Stage configuration



    # ------------------------------------------------------------



    kpi_raw = (



        stage_config.get("KPI_APPLICABLE")



        or stage_config.get("KPIApplicable")



        or stage_config.get("KPI_APPLICABLE_FLAG")



        or ""



    )



    kpi_applicable = (



        "Y"



        if _normalise(kpi_raw)



        in {"y", "yes", "1", "true"}



        else "N"



    )



    # ------------------------------------------------------------



    # Build the new Z_Runner360 row



    # ------------------------------------------------------------



    pk = _build_unique_pk(



        task_date,



        task_start_time,



        runner,



    )



    insert_payload = {



        # ---------------- Identity ----------------



        # RowID is IDENTITY and must NOT be supplied.



        "PK": pk,



        # ---------------- Team Lead Inputs --------



        "I_Branch": branch,



        "I_Runner": runner,



        "I_TaskStartDateTime": task_start_datetime,



        "I_LeadTimeMins": _text(lead_time_input),



        "I_Purpose": purpose,



        # ---------------- S1 Planned ---------------



        "S1_PLHelper": "Runner360 Expected Return Time",



        "S1_PLBaseTS": planned_base_ts,



        "S1_LeadTimeMins": lead_time_mins,



        "S1_PLTimeStamp": planned_timestamp,



        "S1_Doer": runner,



        # ---------------- S1 Actual -----------------



        # These remain NULL until runner presses IN.



        "S1_ALHelper": None,



        "S1_ALTimeStamp": None,



        "S1_KPIApplicable": kpi_applicable,



        "S1_DelayMins": None,



        "S1_TaskStatus": None,



        # Location fields remain NULL until IN/GPS.



        "S1_RunnerLatitude": None,



        "S1_RunnerLongitude": None,



        "S1_LocationVerified": None,



        "S1_LocationDistanceM": None,



    }



    # ------------------------------------------------------------



    # Resolve the live Z_Runner360 schema before insert. Prefer the actual
    # column keys returned with table rows, because some gateway deployments
    # expose stale/over-broad metadata even when SQL Server rejects a column.
    # The current data table uses the legacy split fields in some environments
    # (I_TaskDate + I_TaskStartTime), so do not assume the combined
    # I_TaskStartDateTime field exists.
    actual_columns: dict[str, str] = {}
    try:
        existing_rows = await table_api.get_table(RUNNER360_TABLE)
        if existing_rows and isinstance(existing_rows[0], dict):
            actual_columns = {
                _column_key(name): str(name)
                for name in existing_rows[0].keys()
                if _text(name)
            }
    except Exception:
        existing_rows = []

    # Metadata is used when the table has no rows or a SELECT cannot expose
    # its column keys. This keeps the form usable on a newly created table.
    if not actual_columns:
        try:
            table_metadata = await metadata_service.get_table_metadata(RUNNER360_TABLE)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Unable to inspect the Z_Runner360 table schema: {exc}",
            ) from exc
        for column in table_metadata or []:
            if not isinstance(column, dict):
                continue
            actual_name = _text(
                column.get("name") or column.get("column_name") or column.get("COLUMN_NAME")
            )
            if actual_name:
                actual_columns[_column_key(actual_name)] = actual_name

    if not actual_columns:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not determine the live Z_Runner360 columns. "
                "Task was not inserted; check the table metadata API."
            ),
        )

    # Include legacy split date/time fields as candidates. Prefer the split
    # representation whenever both fields exist. This avoids sending
    # I_TaskStartDateTime to databases whose actual table only has I_TaskDate
    # and I_TaskStartTime, even if gateway metadata incorrectly lists the
    # combined field.
    combined_start_aliases = (
        "I_TaskStartDateTime", "TaskStartDateTime",
        "I_TaskStartTimestamp", "TaskStartTimestamp",
    )
    split_date_aliases = ("I_TaskDate", "TaskDate", "I_StartDate")
    split_time_aliases = ("I_TaskStartTime", "TaskStartTime", "I_StartTime")
    actual_split_date = next((actual_columns.get(_column_key(n)) for n in split_date_aliases if actual_columns.get(_column_key(n))), None)
    actual_split_time = next((actual_columns.get(_column_key(n)) for n in split_time_aliases if actual_columns.get(_column_key(n))), None)
    actual_combined_start = next((actual_columns.get(_column_key(n)) for n in combined_start_aliases if actual_columns.get(_column_key(n))), None)

    insert_candidates = dict(insert_payload)
    # Never copy a combined datetime candidate blindly. Add exactly one start
    # representation below, selected from the table's real column names.
    for alias in combined_start_aliases:
        insert_candidates.pop(alias, None)
    insert_candidates.update({
        "I_ExpectedReturnTime": expected_return_time,
        "I_ExpectedReturnDateTime": planned_timestamp,
    })
    if actual_split_date and actual_split_time:
        insert_candidates[actual_split_date] = task_date
        insert_candidates[actual_split_time] = task_start_time
    elif actual_combined_start:
        insert_candidates[actual_combined_start] = task_start_datetime
    # If neither input format exists but S1_PLBaseTS exists, that stage column
    # remains a supported fallback checked below.

    schema_insert_payload: dict[str, Any] = {}
    for requested_name, value in insert_candidates.items():
        actual_name = actual_columns.get(_column_key(requested_name))
        if actual_name:
            schema_insert_payload[actual_name] = value

    def _put_first_schema_alias(names: tuple[str, ...], value: Any) -> str | None:
        """Map a value to the first supported real column name."""
        for requested_name in names:
            actual_name = actual_columns.get(_column_key(requested_name))
            if actual_name:
                schema_insert_payload[actual_name] = value
                return actual_name
        return None

    # Support deployments with non-prefixed/older names for the basic form
    # fields while preferring the known I_* names when they exist.
    _put_first_schema_alias(("I_Branch", "Branch", "BranchName"), branch)
    _put_first_schema_alias(("I_Runner", "Runner", "RunnerName"), runner)
    _put_first_schema_alias(("I_Purpose", "Purpose", "TaskPurpose"), purpose)
    _put_first_schema_alias(("I_LeadTimeMins", "LeadTimeMins", "I_LeadTimeMinutes"), _text(lead_time_input))
    if actual_split_date and actual_split_time:
        _put_first_schema_alias(split_date_aliases, task_date)
        _put_first_schema_alias(split_time_aliases, task_start_time)
    elif actual_combined_start:
        _put_first_schema_alias(combined_start_aliases, task_start_datetime)
    _put_first_schema_alias(("I_ExpectedReturnTime", "ExpectedReturnTime", "I_ExpectedTime"), expected_return_time)
    _put_first_schema_alias(("I_ExpectedReturnDateTime", "ExpectedReturnDateTime"), planned_timestamp)

    mapped_keys = {_column_key(name) for name in schema_insert_payload}
    has_start = (
        any(key in mapped_keys for key in (
            "itaskstartdatetime", "taskstartdatetime", "itaskstarttimestamp", "taskstarttimestamp",
        ))
        or (any(key in mapped_keys for key in ("itaskdate", "taskdate", "istartdate"))
            and any(key in mapped_keys for key in ("itaskstarttime", "taskstarttime", "istarttime")))
        or "s1plbasets" in mapped_keys
    )
    has_lead = any(key in mapped_keys for key in (
        "ileadtimemins", "leadtimemins", "ileadtimeminutes", "s1leadtimemins",
    ))
    has_planned_return = any(key in mapped_keys for key in (
        "s1pltimestamp", "iexpectedreturntime", "expectedreturntime",
        "iexpectedreturndatetime", "expectedreturndatetime",
    ))
    missing_schema_fields = []
    if not any(key in mapped_keys for key in ("ibranch", "branch", "branchname")):
        missing_schema_fields.append("branch")
    if not any(key in mapped_keys for key in ("irunner", "runner", "runnername")):
        missing_schema_fields.append("runner")
    if not any(key in mapped_keys for key in ("ipurpose", "purpose", "taskpurpose")):
        missing_schema_fields.append("purpose")
    if not has_start:
        missing_schema_fields.append("task start date/time")
    if not has_lead:
        missing_schema_fields.append("lead time")
    if not has_planned_return:
        missing_schema_fields.append("planned return time")
    if missing_schema_fields:
        raise HTTPException(
            status_code=500,
            detail=(
                "Z_Runner360 schema is missing supported columns for: "
                + ", ".join(missing_schema_fields)
                + ". No task was inserted. Check the live Z_Runner360 column metadata."
            ),
        )

    if not schema_insert_payload:
        raise HTTPException(status_code=500, detail="No Z_Runner360 insert columns matched the live schema.")

    # Insert into Z_Runner360 using existing gateway



    # ------------------------------------------------------------



    try:



        insert_result = await table_api.insert_row(



            RUNNER360_TABLE,



            schema_insert_payload,



        )



    except Exception as exc:



        raise HTTPException(



            status_code=502,



            detail=f"Unable to create Runner360 task: {exc}",



        ) from exc



    # ------------------------------------------------------------



    # Read the inserted row back using PK



    # ------------------------------------------------------------



    try:



        inserted_rows = await table_api.get_table(



            RUNNER360_TABLE,



            filters={"PK": pk},



        )



    except Exception as exc:



        raise HTTPException(



            status_code=502,



            detail=(



                "Runner360 task was inserted, but the created record "



                f"could not be read back: {exc}"



            ),



        ) from exc



    if not inserted_rows:



        raise HTTPException(



            status_code=502,



            detail=(



                "Runner360 insert completed, but the created record "



                "could not be located using its PK."



            ),



        )



    created_row = inserted_rows[0]



    row_id = _lookup_case_insensitive(



        created_row,



        "RowID",



    )



    if row_id in (None, ""):



        raise HTTPException(



            status_code=502,



            detail=(



                "Runner360 record was created, but RowID could not "



                "be resolved."



            ),



        )



    # ------------------------------------------------------------



    # This rollout is intentionally scoped to Test_OperationsDB. Always create
    # the signed link for the task in the active SSO database; public link requests
    # are pinned to Test_OperationsDB by OnePortalAuthMiddleware below.
    checkin_link = ""
    try:
        token = _create_mobile_checkin_token(
            str(row_id), _text(selected_assignment.get("employee_code"))
        )
        checkin_link = f"{_mobile_checkin_base_url(request)}/runner360/mobile/{token}"
    except Exception as exc:
        logger.error("Runner360 mobile Check In link could not be created (error_type=%s)", type(exc).__name__)

    # The bulk VM_EmployeeMaster result can omit a populated mobile in the selected
    # row. Before declaring the recipient missing, re-fetch the exact runner by code.
    runner_mobile = _text(selected_assignment.get("mobile_number"))
    if not runner_mobile:
        runner_mobile = await _get_runner360_mobile_by_employee_code(
            _text(selected_assignment.get("employee_code"))
        )

    # Test_OperationsDB is the intended working database for this rollout. When
    # WhatsApp test mode is off, send to the assigned runner's resolved mobile.
    # Best effort after task insert/readback; never turn a saved task into a reported create failure.
    whatsapp_notification = await send_runner360_task_notification(
        task_id=str(row_id),
        runner=runner,
        branch=branch,
        purpose=purpose,
        task_start_datetime=task_start_datetime,
        expected_return_time=expected_return_time,
        lead_time_mins=lead_time_mins,
        runner_mobile=runner_mobile,
        checkin_link=checkin_link,
    )

    # Success response



    # ------------------------------------------------------------



    return JSONResponse(



        status_code=201,



        content={



            "ok": True,



            "message": "Runner360 task created successfully.",



            "task_id": str(row_id),



            "row_id": row_id,



            "pk": pk,



            "fms_name": RUNNER360_FMS_NAME,



            "app_id": RUNNER360_APP_ID,



            "table": RUNNER360_TABLE,



            "stage_no": RUNNER360_STAGE_NO,



            "stage_name": RUNNER360_STAGE_NAME,



            "branch": branch,



            "runner": runner,



            "task_date": task_date,



            "task_start_time": task_start_time,



            "expected_return_time": expected_return_time,



            "s1_pl_base_ts": planned_base_ts,



            "s1_pl_timestamp": planned_timestamp,



            "s1_lead_time_mins": lead_time_mins,



            "insert_result": insert_result,

            "whatsapp_notification": whatsapp_notification,



        },



    )
