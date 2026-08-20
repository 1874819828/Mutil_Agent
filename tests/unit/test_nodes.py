from __future__ import annotations

import asyncio
from pathlib import Path

from app.contracts import (
    ChangeSet,
    FileChange,
    FileOperation,
    ReviewDecision,
    ReviewOutcome,
    TaskPlan,
    TestFailure as ContractTestFailure,
    TestResult as ContractTestResult,
)
from app.llm import AgentRole, DeterministicMockProvider
from app.nodes import DeveloperNode, ManagerNode, PolicyGateNode, ReviewerNode, TesterNode as RoleTesterNode
from app.sandbox import ExecutionResult, RunnerRequest, ScriptedRunner


def test_manager_returns_a_valid_structured_plan() -> None:
    provider = DeterministicMockProvider()
    node = ManagerNode(provider)

    update = asyncio.run(
        node(
            {
                "requirement": "Add a health endpoint",
                "repository_summary": {"language": "python"},
                "effective_change_globs": ["backend/app/**/*.py"],
                "test_profile": "python-fastapi",
            }
        )
    )

    assert isinstance(update["plan"], TaskPlan)
    assert update["plan"].allowed_change_globs == ["backend/app/**/*.py"]
    assert provider.calls[0].role is AgentRole.MANAGER


def test_developer_receives_only_sanitized_test_feedback() -> None:
    change_set = ChangeSet(
        summary="repair health endpoint",
        changes=[
            FileChange(
                operation=FileOperation.CREATE,
                path="backend/app/health.py",
                base_sha256=None,
                content="# fixed\n",
                rationale="implement the requirement",
            )
        ],
    )
    provider = DeterministicMockProvider({AgentRole.DEVELOPER: [change_set]})
    node = DeveloperNode(provider)
    result = ContractTestResult(
        command_id="acceptance_pytest",
        exit_code=1,
        duration_ms=10,
        passed=1,
        failed=1,
        failure_summary=[ContractTestFailure(test="test_health", reason="expected HTTP 200")],
        stdout_artifact="secret-assertion-artifact",
        stderr_artifact=None,
    )

    update = asyncio.run(
        node(
            {
                "plan": _plan(),
                "repository_files": {"backend/app/main.py": "from fastapi import FastAPI\n"},
                "latest_test_result": result,
                "developer_run_count": 1,
            }
        )
    )

    payload = provider.calls[-1].payload
    assert isinstance(update["change_set"], ChangeSet)
    assert update["developer_run_count"] == 2
    assert payload["test_feedback"] == [
        {"test": "test_health", "reason": "expected HTTP 200"}
    ]
    assert "secret-assertion-artifact" not in repr(payload)


def test_tester_uses_injected_runner_and_returns_structured_result(tmp_path: Path) -> None:
    execution = ExecutionResult(
        exit_code=1,
        duration_ms=21,
        stdout="1 failed",
        stderr="",
        passed=0,
        failed=1,
        failures=(("test_health", "expected HTTP 200"),),
    )
    runner = ScriptedRunner({"acceptance_pytest": [execution]})
    node = RoleTesterNode(runner, "acceptance_pytest")
    request = _runner_request(tmp_path, "acceptance_pytest")

    update = node({"runner_requests": {"acceptance_pytest": request}, "test_run_count": 0})

    assert isinstance(update["acceptance_test_result"], ContractTestResult)
    assert update["acceptance_test_result"].failed == 1
    assert update["test_run_count"] == 1


def test_reviewer_returns_a_structured_decision() -> None:
    decision = ReviewDecision(
        decision=ReviewOutcome.APPROVE,
        requirement_coverage=[],
        findings=[],
        residual_risks=[],
        summary="all deterministic gates and tests passed",
    )
    provider = DeterministicMockProvider({AgentRole.REVIEWER: [decision]})
    node = ReviewerNode(provider)

    update = asyncio.run(
        node(
            {
                "requirement": "Add a health endpoint",
                "plan": _plan(),
                "diff": "diff --git a/main.py b/main.py",
                "changed_files": ["backend/app/main.py"],
                "baseline_test_result": _passing_result("baseline_pytest"),
                "acceptance_test_result": _passing_result("acceptance_pytest"),
            }
        )
    )

    assert update["review"] == decision
    assert provider.calls[-1].role is AgentRole.REVIEWER


def test_policy_gate_cannot_be_overridden_by_a_passing_review() -> None:
    gate = PolicyGateNode(max_patch_bytes=20)

    update = gate(
        {
            "baseline_test_result": _passing_result("baseline_pytest"),
            "acceptance_test_result": _passing_result("acceptance_pytest"),
            "baseline_modified": True,
            "diff": "x" * 21,
        }
    )

    assert update["policy_passed"] is False
    assert "immutable baseline tests were modified" in update["policy_violations"]
    assert "patch exceeds the deterministic size limit" in update["policy_violations"]


def _plan() -> TaskPlan:
    return TaskPlan(
        summary="add health endpoint",
        acceptance_criteria=["returns health status"],
        files_to_inspect=["backend/app/main.py"],
        allowed_change_globs=["backend/app/**/*.py"],
        steps=[{"id": "T1", "description": "implement endpoint", "depends_on": []}],
        risk_level="low",
        test_profile="python-fastapi",
    )


def _passing_result(command_id: str) -> ContractTestResult:
    return ContractTestResult(
        command_id=command_id,
        exit_code=0,
        duration_ms=1,
        passed=1,
        failed=0,
        failure_summary=[],
        stdout_artifact=None,
        stderr_artifact=None,
    )


def _runner_request(tmp_path: Path, command_id: str) -> RunnerRequest:
    paths = [tmp_path / name for name in ("workspace", "baseline", "acceptance", "output")]
    for path in paths:
        path.mkdir()
    return RunnerRequest(
        run_id="run-1",
        lease_generation=1,
        command_id=command_id,
        workspace=paths[0],
        baseline_tests=paths[1],
        acceptance_tests=paths[2],
        output_dir=paths[3],
    )
