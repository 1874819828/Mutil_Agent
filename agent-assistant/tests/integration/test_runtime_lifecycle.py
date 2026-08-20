from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config.llm_settings import LLMSettings, ProviderMode
from app.contracts import RunStatus
from app.llm import DeterministicMockProvider
from app.runtime import AssistantRuntime, RuntimeConfigurationError
from app.sandbox import ExecutionResult, ScriptedRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE = PROJECT_ROOT / "config" / "projects" / "student-management-backend.yaml"


def _passing_runner() -> ScriptedRunner:
    return ScriptedRunner(
        {
            "baseline_test": [
                ExecutionResult(
                    exit_code=0,
                    duration_ms=12,
                    stdout="1 passed",
                    stderr="",
                    passed=1,
                )
            ],
            "acceptance_test": [
                ExecutionResult(
                    exit_code=0,
                    duration_ms=18,
                    stdout="2 passed",
                    stderr="",
                    passed=2,
                )
            ],
        }
    )


def _deepseek_settings() -> LLMSettings:
    return LLMSettings.model_validate(
        {
            "provider_mode": ProviderMode.DEEPSEEK,
            "api_key": "test-only-secret",
            "base_url": "https://api.deepseek.com",
            "default_model": "deepseek-chat",
        }
    )


def test_deepseek_run_cannot_bypass_ready_acceptance_pack_requirement(
    tmp_path: Path,
) -> None:
    runtime = AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=_passing_runner(),
        provider=DeterministicMockProvider(),
        llm_settings=_deepseek_settings(),
        acceptance_registry=None,
    )

    with pytest.raises(
        RuntimeConfigurationError,
        match="real LLM runs require a ready acceptance_pack_id",
    ):
        runtime.create_run(
            project_id="student-management-backend",
            requirement="Add a database-aware health endpoint to the backend",
        )


def test_mock_run_pauses_for_approval_then_completes_without_touching_source(
    tmp_path: Path,
) -> None:
    runner = _passing_runner()
    runtime = AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=runner,
        worker_id="integration-worker",
    )

    created = runtime.create_run(
        project_id="student-management-backend",
        requirement="Add a database-aware health endpoint to the backend",
    )
    source_before = runtime.source_manifest_digest(created.run_id)

    waiting = runtime.process_next()
    assert waiting is not None
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert waiting.plan is not None
    assert waiting.plan_hash is not None

    runtime.approve_run(
        waiting.run_id,
        decision="approve",
        plan_hash=waiting.plan_hash,
        project_profile_hash=waiting.project_profile_hash,
        source_manifest_hash=waiting.source_manifest_hash,
        context_bundle_hash=waiting.context_bundle_hash,
        idempotency_key="integration-approval-1",
    )
    completed = runtime.process_next()

    assert completed is not None
    assert completed.status is RunStatus.COMPLETED
    assert [request.command_id for request in runner.calls] == [
        "baseline_test",
        "acceptance_test",
    ]
    assert 'GET /api/v1/health' not in runtime.read_source_file(
        created.run_id, "backend/app/main.py"
    )
    assert '@app.get("/api/v1/health")' in runtime.get_diff(created.run_id)
    assert runtime.source_manifest_digest(created.run_id) == source_before
    report = runtime.get_report(created.run_id)
    assert report["status"] == "completed"
    assert report["policy_passed"] is True


def test_stale_profile_hash_cannot_approve(tmp_path: Path) -> None:
    runtime = AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=_passing_runner(),
        worker_id="integration-worker",
    )
    run = runtime.create_run(
        project_id="student-management-backend",
        requirement="Add a database-aware health endpoint to the backend",
    )
    waiting = runtime.process_next()
    assert waiting is not None and waiting.plan_hash

    try:
        runtime.approve_run(
            run.run_id,
            decision="approve",
            plan_hash=waiting.plan_hash,
            project_profile_hash="0" * 64,
            source_manifest_hash=waiting.source_manifest_hash,
            context_bundle_hash=waiting.context_bundle_hash,
            idempotency_key="integration-stale-profile",
        )
    except Exception as exc:
        assert "profile" in str(exc).lower()
    else:
        raise AssertionError("stale profile approval unexpectedly succeeded")


def test_runtime_finalizes_a_queued_cancellation(tmp_path: Path) -> None:
    runtime = AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=_passing_runner(),
        worker_id="integration-worker",
    )
    run = runtime.create_run(
        project_id="student-management-backend",
        requirement="Do not start this task",
    )
    requested = runtime.request_cancel(run.run_id)

    cancelled = runtime.process_next()

    assert requested.status is RunStatus.CANCEL_REQUESTED
    assert cancelled is not None
    assert cancelled.status is RunStatus.CANCELLED


def test_running_cancellation_immediately_targets_exact_generation(tmp_path: Path) -> None:
    class RecordingRunner(ScriptedRunner):
        def __init__(self) -> None:
            super().__init__({})
            self.cancellations: list[tuple[str, int]] = []

        def cancel(self, run_id: str, lease_generation: int) -> None:
            self.cancellations.append((run_id, lease_generation))

    runner = RecordingRunner()
    runtime = AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=runner,
        worker_id="integration-worker",
    )
    run = runtime.create_run(
        project_id="student-management-backend",
        requirement="Cancel the active exact generation",
    )
    claim = runtime.store.claim_next_run(
        "integration-worker", lease_ttl=timedelta(seconds=30)
    )
    assert claim is not None

    requested = runtime.request_cancel(run.run_id, reason="operator stopped it")

    assert requested.status is RunStatus.CANCEL_REQUESTED
    assert requested.terminal_reason == "operator stopped it"
    assert runner.cancellations == [(run.run_id, claim.token.generation)]


def test_generations_use_disjoint_workspaces(tmp_path: Path) -> None:
    runtime = AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=_passing_runner(),
    )
    profile = runtime._load_profile("student-management-backend")

    first = runtime._workspace(profile, "run-generation-test", 1)
    second = runtime._workspace(profile, "run-generation-test", 2)
    stale_file = first.workspace_root / "backend" / "app" / "main.py"
    active_file = second.workspace_root / "backend" / "app" / "main.py"
    active_before = active_file.read_bytes()

    stale_file.write_text("STALE WRITE", encoding="utf-8")

    assert first.workspace_root != second.workspace_root
    assert active_file.read_bytes() == active_before


def test_crash_after_change_recovers_in_a_fresh_generation(tmp_path: Path) -> None:
    class CrashSignal(BaseException):
        pass

    class CrashAfterChangeRunner:
        def preflight(self) -> str:
            return "crash-runner"

        def run(self, request):
            raise CrashSignal("simulated process loss after apply_change")

        def cancel(self, run_id: str, lease_generation: int) -> None:
            del run_id, lease_generation

    runtime = AssistantRuntime(
        runtime_root=tmp_path / "runtime",
        profile_paths={"student-management-backend": PROFILE},
        runner=_passing_runner(),
        worker_id="recovery-worker",
        lease_seconds=1,
    )
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    runtime.store._clock = lambda: now
    run = runtime.create_run(
        project_id="student-management-backend",
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
        idempotency_key="crash-recovery-approval",
    )

    claim = runtime.store.claim_next_run(
        "crashed-worker", lease_ttl=timedelta(seconds=1)
    )
    assert claim is not None and claim.token.generation == 2
    runtime._runner = CrashAfterChangeRunner()
    try:
        runtime._resume_approved_run(claim.run, claim.token)
    except CrashSignal:
        pass
    else:
        raise AssertionError("simulated crash did not interrupt the run")
    dirty_attempt = (
        runtime.runtime_root
        / "runs"
        / run.run_id
        / "attempts"
        / "g2"
        / "workspace"
        / "backend"
        / "app"
        / "main.py"
    )
    assert '@app.get("/api/v1/health")' in dirty_attempt.read_text(encoding="utf-8")

    now = now + timedelta(seconds=2)
    runtime._runner = _passing_runner()
    completed = runtime.process_next()

    assert completed is not None
    assert completed.status is RunStatus.COMPLETED
    assert completed.lease_generation == 3
    assert (
        runtime.runtime_root
        / "runs"
        / run.run_id
        / "attempts"
        / "g3"
        / "workspace"
    ).exists()
