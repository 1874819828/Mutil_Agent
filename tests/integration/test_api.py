from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import AssistantRuntime
from app.sandbox import ExecutionResult, ScriptedRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE = PROJECT_ROOT / "config" / "projects" / "student-management-backend.yaml"
CONTROL_TOKEN = "test-control-token-that-is-at-least-32-characters"
AUTH_HEADERS = {"Authorization": f"Bearer {CONTROL_TOKEN}"}


def runtime_for_test(tmp_path: Path) -> AssistantRuntime:
    runner = ScriptedRunner(
        {
            "baseline_test": [
                ExecutionResult(0, 10, "1 passed", "", passed=1)
            ],
            "acceptance_test": [
                ExecutionResult(0, 10, "2 passed", "", passed=2)
            ],
        }
    )
    return AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=runner,
        worker_id="api-test-worker",
    )


def test_api_create_approve_and_read_results(tmp_path: Path) -> None:
    runtime = runtime_for_test(tmp_path)
    app = create_app(
        runtime=runtime, start_worker=False, control_token=CONTROL_TOKEN
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        response = client.post(
            "/api/v1/runs",
            headers=AUTH_HEADERS,
            json={
                "project_id": "student-management-backend",
                "requirement": "Add a database-aware health endpoint to the backend",
            },
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        waiting = runtime.process_next()
        assert waiting is not None and waiting.plan_hash
        approval = client.post(
            f"/api/v1/runs/{run_id}/approval",
            headers=AUTH_HEADERS,
            json={
                "decision": "approve",
                "plan_hash": waiting.plan_hash,
                "project_profile_hash": waiting.project_profile_hash,
                "source_manifest_hash": waiting.source_manifest_hash,
                "context_bundle_hash": waiting.context_bundle_hash,
                "idempotency_key": "api-approval-001",
            },
        )
        assert approval.status_code == 200
        runtime.process_next()

        run = client.get(f"/api/v1/runs/{run_id}", headers=AUTH_HEADERS)
        assert run.status_code == 200
        assert run.json()["status"] == "completed"
        diff = client.get(f"/api/v1/runs/{run_id}/diff", headers=AUTH_HEADERS)
        assert diff.status_code == 200
        assert "/api/v1/health" in diff.text
        events = client.get(f"/api/v1/runs/{run_id}/events", headers=AUTH_HEADERS)
        assert events.status_code == 200
        assert any(item["event_type"] == "manager.plan_created" for item in events.json())
        calls = client.get(
            f"/api/v1/runs/{run_id}/llm-calls", headers=AUTH_HEADERS
        )
        assert calls.status_code == 200
        assert {item["role"] for item in calls.json()} >= {
            "manager", "developer", "reviewer"
        }
        artifacts = client.get(
            f"/api/v1/runs/{run_id}/artifacts", headers=AUTH_HEADERS
        )
        assert artifacts.status_code == 200
        assert len(artifacts.json()) >= 4
        artifact_id = artifacts.json()[0]["artifact_id"]
        downloaded = client.get(
            f"/api/v1/artifacts/{artifact_id}", headers=AUTH_HEADERS
        )
        assert downloaded.status_code == 200

        _, artifact_path = runtime.artifact_path(artifact_id)
        artifact_path.write_bytes(b"tampered")
        rejected = client.get(
            f"/api/v1/artifacts/{artifact_id}", headers=AUTH_HEADERS
        )
        assert rejected.status_code == 404


def test_artifact_endpoint_never_accepts_a_raw_path(tmp_path: Path) -> None:
    runtime = runtime_for_test(tmp_path)
    app = create_app(
        runtime=runtime, start_worker=False, control_token=CONTROL_TOKEN
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        response = client.get(
            "/api/v1/artifacts/..%2F..%2F.env", headers=AUTH_HEADERS
        )
        assert response.status_code in {404, 422}


def test_control_api_requires_loopback_bearer_and_trusted_host(tmp_path: Path) -> None:
    runtime = runtime_for_test(tmp_path)
    app = create_app(
        runtime=runtime, start_worker=False, control_token=CONTROL_TOKEN
    )

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as local:
        assert local.get("/api/v1/runs/unknown").status_code == 401
        assert local.get(
            "/api/v1/runs/unknown",
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("192.0.2.10", 50000),
    ) as remote:
        assert remote.get(
            "/api/v1/runs/unknown", headers=AUTH_HEADERS
        ).status_code == 403

    with TestClient(
        app,
        base_url="http://evil.example",
        client=("127.0.0.1", 50000),
    ) as bad_host:
        assert bad_host.get("/health").status_code == 400


def test_console_overview_lists_runs_without_plans(tmp_path: Path) -> None:
    runtime = runtime_for_test(tmp_path)
    app = create_app(
        runtime=runtime, start_worker=False, control_token=CONTROL_TOKEN
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        empty = client.get("/api/v1/runs", headers=AUTH_HEADERS)
        assert empty.status_code == 200
        assert empty.json() == []

        first = client.post(
            "/api/v1/runs",
            headers=AUTH_HEADERS,
            json={
                "project_id": "student-management-backend",
                "requirement": "Add a database-aware health endpoint to the backend",
            },
        )
        second = client.post(
            "/api/v1/runs",
            headers=AUTH_HEADERS,
            json={
                "project_id": "student-management-backend",
                "requirement": "Add grade filtering to the class list endpoint",
            },
        )
        assert first.status_code == 202
        assert second.status_code == 202

        listed = client.get("/api/v1/runs", headers=AUTH_HEADERS)
        assert listed.status_code == 200
        items = listed.json()
        assert [item["run_id"] for item in items] == [
            second.json()["run_id"],
            first.json()["run_id"],
        ]
        assert all("plan" not in item for item in items)
        assert all("requirement" in item for item in items)

        paged = client.get("/api/v1/runs?limit=1", headers=AUTH_HEADERS)
        assert paged.status_code == 200
        assert len(paged.json()) == 1

        invalid = client.get("/api/v1/runs?limit=0", headers=AUTH_HEADERS)
        assert invalid.status_code == 422


def test_console_static_pages_are_served(tmp_path: Path) -> None:
    runtime = runtime_for_test(tmp_path)
    app = create_app(
        runtime=runtime, start_worker=False, control_token=CONTROL_TOKEN
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "Multi-Agent" in index.text
        console = client.get("/console/")
        assert console.status_code == 200
        script = client.get("/console/app.js")
        assert script.status_code == 200
        css = client.get("/console/style.css")
        assert css.status_code == 200

        # 防回归: 每个视图元素 id 都必须能被 showView 精确切换,
        # 否则该视图永远停留在 hidden 状态(历史 bug: "login" vs "loginView")
        view_ids = set(
            re.findall(r'<section id="(loginView|listView|createView|detailView)"', console.text)
        )
        called_views = set(re.findall(r'showView\("([a-zA-Z]+)"\)', script.text))
        assert view_ids == called_views

        # 资源必须使用绝对路径, 根路径与 /console/ 两个入口都能加载
        assert 'href="/console/style.css"' in console.text
        assert 'src="/console/app.js"' in console.text

        # 主题切换: head 内联脚本预应用偏好 + CSS 提供绿白浅色主题 + 切换按钮
        assert "mvd.theme" in console.text
        assert 'data-theme="light"' in css.text
        assert "themeToggleBtn" in console.text
        assert "toggleTheme" in script.text

        # 详情页操作栏吸顶: sticky 偏移使用 JS 实测 header 高度
        assert ".detail-head" in css.text
        assert "position: sticky" in css.text
        assert "--header-h" in css.text
        assert "syncHeaderHeight" in script.text

        # 受 Bearer 保护的制品必须由 fetch 注入令牌后下载,不能使用裸链接。
        assert "downloadArtifact" in script.text
        assert 'href="/api/v1/artifacts/' not in script.text

        # completed 与 published 是两个独立状态,控制台必须显示二次确认入口。
        assert "/publication" in script.text
        assert "publishConfirmCheck" in script.text
