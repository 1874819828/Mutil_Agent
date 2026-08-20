from __future__ import annotations

import os
import secrets

import httpx
import pytest


if "SUT_BASE_URL" not in os.environ:
    pytest.skip("black-box acceptance runs only in the Docker driver", allow_module_level=True)
BASE_URL = os.environ["SUT_BASE_URL"]


def _client() -> tuple[httpx.Client, dict[str, str]]:
    client = httpx.Client(base_url=BASE_URL, timeout=10)
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert response.status_code == 200, response.text
    return client, {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_student_list_supports_whitelisted_sorting() -> None:
    client, headers = _client()
    suffix = secrets.token_hex(4)
    created: dict[str, str] = {}
    for student_no, name in ((f"Z{suffix}", "Sort Z"), (f"A{suffix}", "Sort A")):
        response = client.post(
            "/api/students", headers=headers,
            json={"student_no": student_no, "name": name},
        )
        assert response.status_code == 201, response.text
        created[student_no] = response.json()["id"]
    ascending = client.get(
        "/api/students", headers=headers,
        params={"sort_by": "student_no", "sort_order": "asc", "page_size": 100},
    )
    assert ascending.status_code == 200, ascending.text
    ids = [item["id"] for item in ascending.json()["items"]]
    assert ids.index(created[f"A{suffix}"]) < ids.index(created[f"Z{suffix}"])
    descending = client.get(
        "/api/students", headers=headers,
        params={"sort_by": "student_no", "sort_order": "desc", "page_size": 100},
    )
    assert descending.status_code == 200, descending.text
    ids = [item["id"] for item in descending.json()["items"]]
    assert ids.index(created[f"Z{suffix}"]) < ids.index(created[f"A{suffix}"])


def test_sort_parameters_reject_non_whitelisted_values() -> None:
    client, headers = _client()
    assert client.get(
        "/api/students", headers=headers, params={"sort_by": "password_hash"}
    ).status_code == 422
    assert client.get(
        "/api/students", headers=headers, params={"sort_order": "sideways"}
    ).status_code == 422
