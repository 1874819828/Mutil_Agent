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


def test_grade_exact_filter_preserves_unfiltered_behavior() -> None:
    client, headers = _client()
    suffix = secrets.token_hex(4)
    first = client.post(
        "/api/classes", headers=headers,
        json={"name": f"grade-a-{suffix}", "grade": 2031},
    )
    second = client.post(
        "/api/classes", headers=headers,
        json={"name": f"grade-b-{suffix}", "grade": 2032},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_id, second_id = first.json()["id"], second.json()["id"]
    all_classes = client.get("/api/classes", headers=headers)
    assert all_classes.status_code == 200, all_classes.text
    assert {first_id, second_id}.issubset({item["id"] for item in all_classes.json()})
    filtered = client.get("/api/classes", headers=headers, params={"grade": 2031})
    assert filtered.status_code == 200, filtered.text
    filtered_ids = {item["id"] for item in filtered.json()}
    assert first_id in filtered_ids and second_id not in filtered_ids
    assert all(item["grade"] == 2031 for item in filtered.json())


def test_invalid_grade_returns_422() -> None:
    client, headers = _client()
    response = client.get("/api/classes", headers=headers, params={"grade": "bad"})
    assert response.status_code == 422, response.text
