from datetime import date

import pytest

from app.services import employee_availability_service as availability

WORK_DATE = date(2026, 10, 17)


def attendance(code, status, inserted, day="2026-10-17"):
    return {
        "EmployeeCode": code,
        "AttendanceDate": day,
        "Status": status,
        "InsertTS": inserted,
    }


def test_absent_status_is_trimmed_and_case_insensitive():
    rows = [attendance("E001", " a ", "2026-10-17 08:00:00")]
    assert availability.resolve_attendance_status(rows, "E001", WORK_DATE) == "A"


def test_latest_present_supersedes_earlier_absent_same_day():
    rows = [
        attendance("E001", "A", "2026-10-17 08:00:00"),
        attendance("E001", " P ", "2026-10-17 09:00:00"),
    ]
    assert availability.resolve_attendance_status(rows, "E001", WORK_DATE) == "P"


def test_historical_absent_does_not_block_current_date_without_current_record():
    rows = [attendance("E001", "A", "2026-10-16 08:00:00", day="2026-10-16")]
    assert availability.resolve_attendance_status(rows, "E001", WORK_DATE) == availability.ATTENDANCE_NOT_MARKED


def test_missing_attendance_record_is_not_absent():
    assert availability.resolve_attendance_status([], "E001", WORK_DATE) == availability.ATTENDANCE_NOT_MARKED


def test_conflicting_latest_records_fail_closed():
    rows = [
        attendance("E001", "A", "2026-10-17 09:00:00"),
        attendance("E001", "P", "2026-10-17 09:00:00"),
    ]
    with pytest.raises(availability.EmployeeAvailabilityError, match="Conflicting attendance records"):
        availability.resolve_attendance_status(rows, "E001", WORK_DATE)


@pytest.mark.asyncio
async def test_new_assignment_to_absent_employee_is_rejected(monkeypatch):
    employees = [{"EmployeeCode": "E001", "EmployeeID": "17", "EmployeeName": "Alex Worker"}]
    rows = [attendance("E001", "A", "2026-10-17 08:00:00")]

    async def fake_get_table(table_name, filters=None):
        if table_name == "VM_EmployeeMaster":
            return employees
        if table_name == "Z_AttendanceInfo":
            return rows
        raise AssertionError(f"Unexpected table read: {table_name}")

    monkeypatch.setattr(availability.table_api, "get_table", fake_get_table)
    with pytest.raises(
        availability.EmployeeAssignmentDenied,
        match="This employee is marked Absent in Z_AttendanceInfo and cannot be assigned work",
    ):
        await availability.validate_new_assignment_changes(
            {"S1_Doer": "Alex Worker"}, {}, work_date=WORK_DATE
        )


@pytest.mark.asyncio
async def test_new_assignment_to_present_employee_is_allowed(monkeypatch):
    employees = [{"EmployeeCode": "E001", "EmployeeID": "17", "EmployeeName": "Alex Worker"}]
    rows = [attendance("E001", "P", "2026-10-17 08:00:00")]

    async def fake_get_table(table_name, filters=None):
        if table_name == "VM_EmployeeMaster":
            return employees
        if table_name == "Z_AttendanceInfo":
            return rows
        raise AssertionError(f"Unexpected table read: {table_name}")

    monkeypatch.setattr(availability.table_api, "get_table", fake_get_table)
    await availability.validate_new_assignment_changes(
        {"S1_Doer": "Alex Worker"}, {}, work_date=WORK_DATE
    )


@pytest.mark.asyncio
async def test_attendance_read_failure_blocks_new_assignment(monkeypatch):
    employees = [{"EmployeeCode": "E001", "EmployeeID": "17", "EmployeeName": "Alex Worker"}]

    async def fake_get_table(table_name, filters=None):
        if table_name == "VM_EmployeeMaster":
            return employees
        if table_name == "Z_AttendanceInfo":
            raise RuntimeError("simulated API failure")
        raise AssertionError(f"Unexpected table read: {table_name}")

    monkeypatch.setattr(availability.table_api, "get_table", fake_get_table)
    with pytest.raises(availability.EmployeeAvailabilityError, match="Attendance status could not be verified"):
        await availability.validate_new_assignment_changes(
            {"S1_Doer": "Alex Worker"}, {}, work_date=WORK_DATE
        )


@pytest.mark.asyncio
async def test_kpi_and_active_trip_make_employee_busy(monkeypatch):
    employees = [{"EmployeeCode": "E001", "EmployeeID": "17", "EmployeeName": "Alex Worker"}]
    tables = {
        "KPI_MasterRecord": [{"DoerCode": "E001", "PLTimestamp": "2026-10-17 09:00:00", "ALTimestamp": None}],
        "Z_KPI_MasterRecord": [{"Doer": "Alex Worker", "S1_PLTimeStamp": "2026-10-17 09:00:00", "S1_ALTimeStamp": None}],
        "FL_Drivers": [{"user_id": 900, "employee_id": 17}],
        "FL_Trips": [{"driver_id": 900, "actual_start_time": "2026-10-17 09:00:00", "completion_time": None}],
    }

    async def fake_get_table(table_name, filters=None):
        if table_name not in tables:
            raise AssertionError(f"Unexpected table read: {table_name}")
        return tables[table_name]

    monkeypatch.setattr(availability.table_api, "get_table", fake_get_table)
    busy = await availability.get_busy_employee_codes(employees)
    assert availability.normalize_key("E001") in busy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kpi_rows,trips,expected_busy",
    [
        ([{"DoerCode": "E001", "PLTimestamp": "2026-10-17 09:00:00", "ALTimestamp": None}], [], True),
        ([{"DoerCode": "E001", "PLTimestamp": "2026-10-17 09:00:00", "ALTimestamp": "2026-10-17 10:00:00"}], [], False),
        ([], [{"driver_id": 900, "actual_start_time": "2026-10-17 09:00:00", "completion_time": None}], True),
        ([], [{"driver_id": 900, "actual_start_time": None, "completion_time": None}], False),
        ([], [{"driver_id": 900, "actual_start_time": "2026-10-17 09:00:00", "completion_time": "2026-10-17 10:00:00"}], False),
    ],
)
async def test_kpi_and_trip_busy_conditions(monkeypatch, kpi_rows, trips, expected_busy):
    employees = [{"EmployeeCode": "E001", "EmployeeID": "17", "EmployeeName": "Alex Worker"}]
    tables = {
        "KPI_MasterRecord": kpi_rows,
        "Z_KPI_MasterRecord": [],
        "FL_Drivers": [{"user_id": 900, "employee_id": 17}],
        "FL_Trips": trips,
    }

    async def fake_get_table(table_name, filters=None):
        if table_name not in tables:
            raise AssertionError(f"Unexpected table read: {table_name}")
        return tables[table_name]

    monkeypatch.setattr(availability.table_api, "get_table", fake_get_table)
    busy = await availability.get_busy_employee_codes(employees)
    assert (availability.normalize_key("E001") in busy) is expected_busy


def test_unknown_status_is_not_assumed_present_or_absent():
    rows = [attendance("E001", "X", "2026-10-17 08:00:00")]
    with pytest.raises(availability.EmployeeAvailabilityError, match="Conflicting attendance records"):
        availability.resolve_attendance_status(rows, "E001", WORK_DATE)


@pytest.mark.asyncio
async def test_same_existing_assignment_is_not_rejected_during_completion(monkeypatch):
    async def should_not_be_called(*args, **kwargs):
        raise AssertionError("unchanged assignment should not trigger attendance validation")

    monkeypatch.setattr(availability.table_api, "get_table", should_not_be_called)
    await availability.validate_new_assignment_changes(
        {"S1_Doer": "Alex Worker"}, {"S1_Doer": "Alex Worker"}, work_date=WORK_DATE
    )


@pytest.mark.asyncio
async def test_picker_dropdown_absent_overrides_busy_and_present_is_available(monkeypatch):
    from app.services import stage1_custom_service as picker_service

    employee_rows = [
        {"EmployeeCode": "E001", "EmployeeID": "17", "EmployeeName": "Alex Worker", "Active": "Y", "Branch": "Noida"},
        {"EmployeeCode": "E002", "EmployeeID": "18", "EmployeeName": "Pat Present", "Active": "Y", "Branch": "Noida"},
    ]
    tables = {
        "VM_EmployeeMaster": employee_rows,
        "VM_Team": [
            {"TeamID": "30", "EmployeeCode": "E001"},
            {"TeamID": "30", "EmployeeCode": "E001"},  # duplicate source row is de-duplicated
            {"TeamID": "30", "EmployeeCode": "E002"},
        ],
        "Z_AttendanceInfo": [attendance("E001", "A", "2026-10-17 08:00:00")],
        "KPI_MasterRecord": [{"DoerCode": "E001", "PLTimestamp": "2026-10-17 08:00:00", "ALTimestamp": None}],
        "Z_KPI_MasterRecord": [],
        "FL_Drivers": [{"user_id": 900, "employee_id": 17}],
        "FL_Trips": [{"driver_id": 900, "actual_start_time": "2026-10-17 08:00:00", "completion_time": None}],
        "Z_SalesOrder": [{"RowID": "1", "S1_PickID": "P0", "S1_Picker": "Alex Worker", "S1_ALTimeStamp": None}],
        "TM_Form_Settings": [],
        "Z_SOPickNpackBatchDetails": [],
    }

    async def fake_get_table(table_name, filters=None):
        if table_name not in tables:
            raise AssertionError(f"Unexpected table read: {table_name}")
        rows = tables[table_name]
        if filters:
            return [row for row in rows if all(str(row.get(key, "")).strip() == str(value).strip() for key, value in filters.items())]
        return rows

    monkeypatch.setattr(picker_service.table_api, "get_table", fake_get_table)
    picker_service.invalidate_picker_assignment_cache()
    options = await picker_service._load_picker_options("Noida", work_date=WORK_DATE)
    by_code = {row["employee_code"]: row for row in options}
    assert by_code["E001"]["status"] == "Absent"
    assert by_code["E001"]["disabled"] is True
    assert by_code["E001"]["is_busy"] is False  # display priority suppresses Busy label
    assert by_code["E002"]["status"] == "Available"
    assert by_code["E002"]["disabled"] is False
    assert sum(1 for row in options if row["employee_code"] == "E001") == 1
    picker_service.invalidate_picker_assignment_cache()


def test_timezone_offsets_are_normalized_before_insert_timestamp_comparison():
    rows = [
        attendance("E001", "A", "2026-10-17T08:00:00+00:00"),
        attendance("E001", "P", "2026-10-17T13:30:00+05:30"),
    ]
    # Both timestamps represent the same instant but conflicting statuses; the
    # latest set is ambiguous and must fail closed instead of raising TypeError.
    with pytest.raises(availability.EmployeeAvailabilityError, match="Conflicting attendance records"):
        availability.resolve_attendance_status(rows, "E001", WORK_DATE)


@pytest.mark.asyncio
async def test_runner360_attendance_absent_overrides_busy(monkeypatch):
    from app.routers import runner360

    async def fake_status_map(codes, work_date=None):
        return {availability.normalize_key("E001"): availability.ATTENDANCE_ABSENT}

    monkeypatch.setattr(runner360.employee_availability_service, "get_attendance_status_map", fake_status_map)
    assignments = [{
        "employee_code": "E001",
        "is_busy": True,
        "busy_check_available": True,
        "availability_error": False,
    }]
    await runner360._apply_runner_attendance_status(assignments, WORK_DATE)
    assert assignments[0]["availability_status"] == "Absent"
    assert assignments[0]["is_absent"] is True
    assert assignments[0]["availability_error"] is False
