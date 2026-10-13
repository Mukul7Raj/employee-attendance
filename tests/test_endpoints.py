"""
Unit Tests for API Endpoints and Schema Validations (Mocked Database)
"""
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


@pytest.fixture
def mock_db():
    original_db = main_module.db
    original_client = main_module.client

    mock_db_instance = MagicMock()
    mock_client_instance = MagicMock()
    main_module.db = mock_db_instance
    main_module.client = mock_client_instance

    yield mock_db_instance, mock_client_instance

    main_module.db = original_db
    main_module.client = original_client


@pytest.fixture
def client(mock_db):
    return TestClient(app, raise_server_exceptions=False)


def test_health_endpoint_healthy(client, mock_db):
    _, mock_client = mock_db
    mock_client.admin.command.return_value = {"ok": 1}

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_endpoint_unhealthy(client, mock_db):
    _, mock_client = mock_db
    mock_client.admin.command.side_effect = Exception("Connection refused")

    resp = client.get("/health")
    assert resp.status_code == 503
    assert "Database unavailable" in resp.json()["detail"]


def test_create_employee_invalid_shift_times(client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 422
    assert "shift_start must differ from shift_end" in resp.json()["detail"]


def test_create_employee_invalid_emp_code(client):
    payload = {
        "emp_code": "INVALID_CODE",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 422


def test_punch_in_seconds_timestamp_rejected(client):
    payload = {
        "emp_code": "EMP0001",
        "punched_at": 1783312500,  # 10 digits = seconds (< 100000000000)
        "status": "PRESENT",
    }
    resp = client.post("/attendance/punch-in", json=payload)
    assert resp.status_code == 422


def test_punch_in_invalid_status_rejected(client):
    payload = {
        "emp_code": "EMP0001",
        "status": "ABSENT",
    }
    resp = client.post("/attendance/punch-in", json=payload)
    assert resp.status_code == 422


def test_attendance_list_date_range_validation(client):
    resp = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert resp.status_code == 422
    assert "date_from must not be greater than date_to" in resp.json()["detail"]


def test_trend_date_validation(client, mock_db):
    mock_database, _ = mock_db
    mock_database.employees.find_one.return_value = {"department": "Engineering"}

    # to < from must be 422
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert resp.status_code == 422
    assert "'to' date cannot be before 'from' date" in resp.json()["detail"]

    # Range > 92 days must be 422
    resp2 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert resp2.status_code == 422
    assert "exceeds 92 days" in resp2.json()["detail"]


def test_explain_missing_parameters_validation(client):
    # employee_monthly without month must be 422
    resp = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert resp.status_code == 422

    # department_summary without month must be 422
    resp2 = client.get("/admin/explain/department_summary")
    assert resp2.status_code == 422
