from __future__ import annotations

import os
import secrets

import httpx
import pytest


if "SUT_BASE_URL" not in os.environ:
    pytest.skip("black-box acceptance runs only in the Docker driver", allow_module_level=True)


def test_class_update_returns_real_student_count() -> None:
    client = httpx.Client(base_url=os.environ["SUT_BASE_URL"], timeout=10)
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    suffix = secrets.token_hex(4)
    created_class = client.post(
        "/api/classes", headers=headers,
        json={"name": f"count-{suffix}", "grade": 2030},
    )
    assert created_class.status_code == 201, created_class.text
    class_id = created_class.json()["id"]
    student = client.post(
        "/api/students", headers=headers,
        json={"student_no": f"C{suffix}", "name": "Count Student", "class_id": class_id},
    )
    assert student.status_code == 201, student.text
    updated = client.put(
        f"/api/classes/{class_id}", headers=headers,
        json={"head_teacher": "New Teacher"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["student_count"] == 1
    assert updated.json()["head_teacher"] == "New Teacher"
