"""Independent acceptance oracle for the health-endpoint golden task.

These tests live outside the writable target workspace. The runner mounts this
directory read-only and never includes its source in Manager/Developer context.
"""

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


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve(strict=False).as_posix()}"


def _probe(database_url: str) -> dict:
    script = """
import json
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
response = client.get('/api/v1/health')
print(json.dumps({'status_code': response.status_code, 'json': response.json()}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND)
    env["DATABASE_URL"] = database_url
    env["SECRET_KEY"] = "golden-test-only-secret"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_health_reports_healthy_database() -> None:
    with tempfile.TemporaryDirectory(prefix="health-acceptance-") as directory:
        result = _probe(_sqlite_url(Path(directory) / "health.db"))
    assert result == {
        "status_code": 200,
        "json": {"status": "ok", "database": "ok"},
    }


def test_health_reports_database_failure_without_leaking_details() -> None:
    with tempfile.NamedTemporaryFile(prefix="health-parent-is-file-") as parent_file:
        invalid_database = Path(parent_file.name) / "health.db"
        result = _probe(_sqlite_url(invalid_database))
    assert result == {
        "status_code": 503,
        "json": {"status": "degraded", "database": "error"},
    }
