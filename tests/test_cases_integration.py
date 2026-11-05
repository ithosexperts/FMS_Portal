import time
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.services.cases_integration import (
    create_fms_launch_token,
    verify_fms_launch_token,
    compute_case_location_from_records,
)

client = TestClient(app)

TEST_SECRET = "test_fms_cases_shared_secret_32_characters_minimum_length!"


@pytest.fixture(autouse=True)
def setup_secret(monkeypatch):
    monkeypatch.setattr(settings, "fms_cases_shared_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "cases_system_url", "https://cases-system.example.com")


def test_create_and_verify_launch_token():
    context = {
        "app_id": "21",
        "task_id": "TASK-1001",
        "stage_no": "S2",
        "stage_name": "Pack to Dispatch",
        "form_name": "Sales Order Form",
        "form_url": "/forms/stage/TASK-1001/S2?app_id=21",
        "database": "OperationsDB",
        "employee_code": "0003",
    }
    token = create_fms_launch_token(context)
    claims = verify_fms_launch_token(token)
    assert claims["app_id"] == "21"
    assert claims["task_id"] == "TASK-1001"
    assert claims["stage_no"] == "S2"
    assert claims["database"] == "OperationsDB"
    assert claims["employee_code"] == "0003"


def test_tampered_launch_token_rejected():
    context = {"app_id": "21", "task_id": "TASK-1001"}
    token = create_fms_launch_token(context)
    parts = token.split(".")
    tampered = f"{parts[0]}.wrongsignature00000000000000000000000000000000000000000000000000"
    with pytest.raises(ValueError) as exc:
        verify_fms_launch_token(tampered)
    assert "signature" in str(exc.value).lower()


def test_case_location_rules():
    # 1. No records -> Live
    assert compute_case_location_from_records([]) == "Live"

    # 2. Future snooze -> Snooze
    future_time = (datetime.now() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
    snooze_records = [{
        "EmployeeCode": "0003",
        "Type": "Snooze",
        "SnoozeDateTime": future_time
    }]
    assert compute_case_location_from_records(snooze_records, employee_code="0003") == "Snooze"

    # 3. Expired snooze -> Live
    past_time = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    expired_records = [{
        "EmployeeCode": "0003",
        "Type": "Snooze",
        "SnoozeDateTime": past_time
    }]
    assert compute_case_location_from_records(expired_records, employee_code="0003") == "Live"

    # 4. Archive -> Archive
    archive_records = [{
        "EmployeeCode": "0003",
        "Type": "Archive",
        "ArchiveDateTime": past_time
    }]
    assert compute_case_location_from_records(archive_records, employee_code="0003") == "Archive"

    # 5. Respect employeeCode filter
    mixed_records = [
        {"EmployeeCode": "0002", "Type": "Archive"},
        {"EmployeeCode": "0003", "Type": "Live"},
    ]
    assert compute_case_location_from_records(mixed_records, employee_code="0003") == "Live"


def test_api_create_launch_token():
    payload = {
        "app_id": "21",
        "task_id": "SO-8888",
        "stage_no": "S1",
        "stage_name": "Order Booking",
        "form_name": "Sales Order",
        "form_url": "/forms/stage/SO-8888/S1?app_id=21",
    }
    response = client.post("/api/fms/cases/launch-token", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "token" in data
    assert "launch_url" in data
    assert "fms_token=" in data["launch_url"]

    # Verify token payload
    claims = verify_fms_launch_token(data["token"])
    assert claims["app_id"] == "21"
    assert claims["task_id"] == "SO-8888"


def test_api_get_linked_cases_empty():
    response = client.get("/api/fms/cases/linked?app_id=NONEXISTENT&task_id=NONE")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["count"] == 0
    assert data["cases"] == []

