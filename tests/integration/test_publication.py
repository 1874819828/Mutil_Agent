from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml
from fastapi.testclient import TestClient

from app.contracts import RunStatus
from app.main import create_app
from app.runtime import AssistantRuntime
from app.sandbox import ExecutionResult, ScriptedRunner


def _result(*, ok: bool = True) -> ExecutionResult:
    return ExecutionResult(
        exit_code=0 if ok else 1,
        duration_ms=5,
        stdout="1 passed" if ok else "1 failed",
        stderr="",
        passed=1 if ok else 0,
        failed=0 if ok else 1,
        failures=() if ok else (("post_publish", "verification failed"),),
    )


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _publish_binding(preview) -> dict[str, str]:
    assert preview.git_current_branch
    assert preview.git_base_commit
    assert preview.git_target_branch
    return {
        "git_original_branch": preview.git_current_branch,
        "git_base_commit": preview.git_base_commit,
        "git_target_branch": preview.git_target_branch,
    }


def _runtime(tmp_path: Path, *, post_publish_ok: bool = True) -> tuple[AssistantRuntime, Path]:
    source = tmp_path / "project"
    (source / "backend" / "app").mkdir(parents=True)
    (source / "backend" / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from app.database import async_session\n\n"
        "app = FastAPI()\n\n"
        "if __name__ == \"__main__\":\n"
        "    pass\n",
        encoding="utf-8",
        newline="",
    )
    (source / "backend" / "app" / "database.py").write_text(
        "async_session = None\n", encoding="utf-8", newline=""
    )
    # Real repositories contain many files outside the controlled profile;
    # publication must ignore them without allowing them to be written.
    (source / "README.md").write_text("outside managed scope\n", encoding="utf-8")
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "add", "--", ".")
    _git(source, "commit", "-m", "initial")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "project_id": "publish-demo",
                "source_path": str(source),
                "include_globs": ["backend/app/**/*.py"],
                "exclude_globs": ["**/__pycache__/**"],
                "runner": "python-fastapi",
                "commands": {
                    "syntax": ["python", "-m", "compileall", "backend/app"],
                    "baseline_test": ["python", "-m", "pytest", "/baseline_tests"],
                    "acceptance_test": ["python", "-m", "pytest", "/acceptance_tests"],
                },
                "runner_image": "runner:test",
                "limits": {
                    "timeout_seconds": 30,
                    "memory_mb": 256,
                    "cpus": 1,
                    "max_files": 20,
                    "max_file_mb": 1,
                    "max_workspace_mb": 2,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runner = ScriptedRunner(
        {
            "baseline_test": [_result(), _result(ok=post_publish_ok)],
            "acceptance_test": [_result(), _result()],
        }
    )
    return (
        AssistantRuntime(
            runtime_root=tmp_path / "runtime",
            profile_paths={"publish-demo": profile},
            runner=runner,
            worker_id="publication-test-worker",
        ),
        source,
    )


def _completed(runtime: AssistantRuntime):
    run = runtime.create_run(
        project_id="publish-demo",
        requirement="Add a database-aware health endpoint to the backend",
    )
    waiting = runtime.process_next()
    assert waiting is not None and waiting.plan_hash
    runtime.approve_run(
        run.run_id,
        decision="approve",
        plan_hash=waiting.plan_hash,
        project_profile_hash=waiting.project_profile_hash,
        source_manifest_hash=waiting.source_manifest_hash,
        context_bundle_hash=waiting.context_bundle_hash,
        idempotency_key="publication-plan-approval",
    )
    completed = runtime.process_next()
    assert completed is not None and completed.status is RunStatus.COMPLETED
    return completed


def test_completed_run_requires_hash_bound_second_confirmation_before_publish(
    tmp_path: Path,
) -> None:
    runtime, source = _runtime(tmp_path)
    completed = _completed(runtime)
    before = (source / "backend" / "app" / "main.py").read_bytes()

    preview = runtime.publication_preview(completed.run_id)

    assert preview.eligible is True
    assert preview.status == "not_published"
    assert preview.changed_files == ("backend/app/main.py",)
    assert preview.diff_sha256
    assert (source / "backend" / "app" / "main.py").read_bytes() == before

    with pytest.raises(RuntimeError, match="diff"):
        runtime.publish_run(
            completed.run_id,
            confirmation="publish",
            project_profile_hash=preview.project_profile_hash,
            source_manifest_hash=preview.source_manifest_hash,
            diff_sha256="0" * 64,
            **_publish_binding(preview),
            idempotency_key="publication-confirm-001",
        )
    assert (source / "backend" / "app" / "main.py").read_bytes() == before

    published = runtime.publish_run(
        completed.run_id,
        confirmation="publish",
        project_profile_hash=preview.project_profile_hash,
        source_manifest_hash=preview.source_manifest_hash,
        diff_sha256=preview.diff_sha256,
        **_publish_binding(preview),
        idempotency_key="publication-confirm-002",
    )

    assert published.status == "published"
    assert published.git_original_branch == "main"
    assert published.git_branch == preview.git_target_branch
    assert published.git_commit == _git(source, "rev-parse", "HEAD")
    assert _git(source, "branch", "--show-current") == preview.git_target_branch
    assert _git(source, "show", "--pretty=format:", "--name-only", "HEAD") == (
        "backend/app/main.py"
    )
    assert '@app.get("/api/v1/health")' in (
        source / "backend" / "app" / "main.py"
    ).read_text(encoding="utf-8")
    assert runtime.publish_run(
        completed.run_id,
        confirmation="publish",
        project_profile_hash=preview.project_profile_hash,
        source_manifest_hash=preview.source_manifest_hash,
        diff_sha256=preview.diff_sha256,
        **_publish_binding(preview),
        idempotency_key="publication-confirm-002",
    ) == published


def test_publication_rejects_source_drift_without_writing(tmp_path: Path) -> None:
    runtime, source = _runtime(tmp_path)
    completed = _completed(runtime)
    main = source / "backend" / "app" / "main.py"
    before = main.read_bytes()
    (source / "backend" / "app" / "database.py").write_text(
        "async_session = 'operator edit'\n", encoding="utf-8", newline=""
    )

    preview = runtime.publication_preview(completed.run_id)

    assert preview.eligible is False
    assert "changed" in (preview.reason or "").lower()
    with pytest.raises(RuntimeError, match="changed"):
        runtime.publish_run(
            completed.run_id,
            confirmation="publish",
            project_profile_hash=preview.project_profile_hash,
            source_manifest_hash=preview.source_manifest_hash,
            diff_sha256=preview.diff_sha256,
            git_original_branch=preview.git_current_branch or "main",
            git_base_commit=preview.git_base_commit or "0" * 40,
            git_target_branch=preview.git_target_branch or "agent/run-missing",
            idempotency_key="publication-drift-001",
        )
    assert main.read_bytes() == before


def test_publication_rejects_git_dirty_path_outside_managed_profile(
    tmp_path: Path,
) -> None:
    runtime, source = _runtime(tmp_path)
    completed = _completed(runtime)
    (source / "README.md").write_text("operator edit\n", encoding="utf-8")

    preview = runtime.publication_preview(completed.run_id)

    assert preview.eligible is False
    assert preview.git_ready is False
    assert "uncommitted" in (preview.reason or "").lower()
    assert _git(source, "branch", "--show-current") == "main"


def test_failed_post_publish_verification_rolls_back_all_files(tmp_path: Path) -> None:
    runtime, source = _runtime(tmp_path, post_publish_ok=False)
    completed = _completed(runtime)
    preview = runtime.publication_preview(completed.run_id)
    before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*.py")
    }

    with pytest.raises(RuntimeError, match="verification"):
        runtime.publish_run(
            completed.run_id,
            confirmation="publish",
            project_profile_hash=preview.project_profile_hash,
            source_manifest_hash=preview.source_manifest_hash,
            diff_sha256=preview.diff_sha256,
            **_publish_binding(preview),
            idempotency_key="publication-rollback-001",
        )

    after = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*.py")
    }
    assert after == before
    record = runtime.store.get_publication(completed.run_id)
    assert record is not None and record.status == "failed"
    assert _git(source, "branch", "--show-current") == "main"
    assert _git(source, "branch", "--list", preview.git_target_branch or "") == ""


def test_publication_api_exposes_preview_and_requires_bound_confirmation(
    tmp_path: Path,
) -> None:
    runtime, source = _runtime(tmp_path)
    completed = _completed(runtime)
    token = "publication-api-control-token-32-chars"
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app(runtime=runtime, start_worker=False, control_token=token)

    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        preview_response = client.get(
            f"/api/v1/runs/{completed.run_id}/publication", headers=headers
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["eligible"] is True

        rejected = client.post(
            f"/api/v1/runs/{completed.run_id}/publication",
            headers=headers,
            json={
                "confirmation": "publish",
                "project_profile_hash": preview["project_profile_hash"],
                "source_manifest_hash": preview["source_manifest_hash"],
                "diff_sha256": "f" * 64,
                "git_original_branch": preview["git_current_branch"],
                "git_base_commit": preview["git_base_commit"],
                "git_target_branch": preview["git_target_branch"],
                "idempotency_key": "publication-api-reject-001",
            },
        )
        assert rejected.status_code == 409

        published = client.post(
            f"/api/v1/runs/{completed.run_id}/publication",
            headers=headers,
            json={
                "confirmation": "publish",
                "project_profile_hash": preview["project_profile_hash"],
                "source_manifest_hash": preview["source_manifest_hash"],
                "diff_sha256": preview["diff_sha256"],
                "git_original_branch": preview["git_current_branch"],
                "git_base_commit": preview["git_base_commit"],
                "git_target_branch": preview["git_target_branch"],
                "idempotency_key": "publication-api-confirm-001",
                "comment": "reviewed in local console",
            },
        )

    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["git_branch"] == preview["git_target_branch"]
    assert published.json()["git_commit"] == _git(source, "rev-parse", "HEAD")
    assert "/api/v1/health" in (
        source / "backend" / "app" / "main.py"
    ).read_text(encoding="utf-8")


def test_interrupted_publication_is_rolled_back_on_runtime_restart(
    tmp_path: Path,
) -> None:
    runtime, source = _runtime(tmp_path)
    completed = _completed(runtime)
    preview = runtime.publication_preview(completed.run_id)
    profile = runtime._verified_profile(completed)
    workspace = runtime._final_workspace(completed)
    diff = runtime.get_diff(completed.run_id)
    attempt_relative = (
        Path("runs") / completed.run_id / "publication" / "attempt-crash-test"
    )
    attempt_root = runtime.runtime_root / attempt_relative
    attempt_root.mkdir(parents=True)
    journal, originals, desired = runtime._prepare_publication_journal(
        profile=profile,
        run=completed,
        workspace=workspace,
        changed_files=preview.changed_files,
        attempt_root=attempt_root,
    )
    journal_path = attempt_root / "journal.json"
    journal_path.write_text(
        json.dumps(journal, sort_keys=True), encoding="utf-8", newline=""
    )
    request_hash = hashlib.sha256(b"crash-test-request").hexdigest()
    runtime.store.begin_publication(
        run_id=completed.run_id,
        project_profile_hash=preview.project_profile_hash,
        source_manifest_hash=preview.source_manifest_hash,
        diff_sha256=preview.diff_sha256,
        request_hash=request_hash,
        idempotency_key="publication-crash-test",
        changed_files=preview.changed_files,
        backup_relative_path=(attempt_relative / "journal.json").as_posix(),
        git_original_branch=preview.git_current_branch or "main",
        git_base_commit=preview.git_base_commit or "0" * 40,
        git_branch=preview.git_target_branch or "agent/run-missing",
    )
    runtime._git_delivery.start(
        source,
        run_id=completed.run_id,
        expected_original_branch=preview.git_current_branch or "main",
        expected_base_commit=preview.git_base_commit or "0" * 40,
        expected_target_branch=preview.git_target_branch or "agent/run-missing",
    )
    target = source / "backend" / "app" / "main.py"
    target.write_bytes(desired["backend/app/main.py"])
    assert target.read_bytes() != originals["backend/app/main.py"]

    restarted = AssistantRuntime(
        runtime_root=runtime.runtime_root,
        profile_paths={"publish-demo": tmp_path / "profile.yaml"},
        runner=ScriptedRunner({}),
        worker_id="publication-recovery-worker",
    )

    assert target.read_bytes() == originals["backend/app/main.py"]
    record = restarted.store.get_publication(completed.run_id)
    assert record is not None and record.status == "failed"
    assert "rolled back" in (record.error or "")
    assert _git(source, "branch", "--show-current") == "main"
    assert _git(source, "branch", "--list", preview.git_target_branch or "") == ""
    assert diff
