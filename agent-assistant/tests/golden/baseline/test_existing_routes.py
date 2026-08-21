"""Immutable smoke oracle for the original student-management backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


if os.getenv("RUN_GOLDEN_TESTS") != "1":
    pytest.skip("golden tests are executed only by the isolated runner", allow_module_level=True)


BACKEND = Path(os.getenv("GOLDEN_BACKEND", "/workspace/backend"))


def test_existing_api_routes_remain_registered() -> None:
    script = """
import json
from app.main import app
print(json.dumps(sorted(route.path for route in app.routes)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    routes = json.loads(result.stdout.strip().splitlines()[-1])
    assert "/api/auth/login" in routes
    assert "/api/students" in routes
    assert "/api/classes" in routes
    assert "/" in routes


def test_login_students_and_classes_smoke_contracts() -> None:
    script = r'''
import json
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    root = client.get("/")
    bad_login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong"}
    )
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    students = client.get("/api/students", headers=headers)
    classes = client.get("/api/classes", headers=headers)
    print(json.dumps({
        "root": [root.status_code, root.json()],
        "bad_login": bad_login.status_code,
        "login": [login.status_code, login.json()],
        "students": [students.status_code, students.json()],
        "classes": [classes.status_code, classes.json()],
    }))
'''
    with tempfile.TemporaryDirectory(prefix="baseline-db-") as temp_dir:
        database = Path(temp_dir) / "baseline.db"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BACKEND)
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )

    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["root"][0] == 200
    assert observed["root"][1]["docs"] == "/docs"
    assert observed["bad_login"] == 401
    assert observed["login"][0] == 200
    assert observed["login"][1]["token_type"] == "bearer"
    assert observed["login"][1]["user"]["username"] == "admin"
    assert observed["students"][0] == 200
    assert observed["students"][1] == {
        "total": 0,
        "page": 1,
        "page_size": 20,
        "items": [],
    }
    assert observed["classes"] == [200, []]
