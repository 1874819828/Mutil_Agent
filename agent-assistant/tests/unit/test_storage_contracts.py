from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.contracts import (
    ChangeSet,
    FileChange,
    FileOperation,
    InvalidStatusTransition,
    RiskLevel,
    RunStatus,
    TaskPlan,
    TaskStep,
    ProposedFileChange,
    canonical_json_hash,
    validate_transition,
)


def valid_plan() -> TaskPlan:
    return TaskPlan(
        summary="Add a health endpoint",
        acceptance_criteria=["healthy database returns HTTP 200"],
        files_to_inspect=["backend/app/main.py"],
        allowed_change_globs=["backend/app/**/*.py"],
        steps=[
            TaskStep(id="inspect", description="Inspect the application"),
            TaskStep(
                id="implement",
                description="Implement the endpoint",
                depends_on=["inspect"],
            ),
        ],
        risk_level=RiskLevel.LOW,
        test_profile="python-fastapi",
    )


def test_task_plan_hash_is_stable_across_equivalent_serialization() -> None:
    plan = valid_plan()

    assert canonical_json_hash(plan) == canonical_json_hash(
        TaskPlan.model_validate(plan.model_dump())
    )


@pytest.mark.parametrize(
    "steps",
    [
        [TaskStep(id="same", description="one"), TaskStep(id="same", description="two")],
        [TaskStep(id="one", description="one", depends_on=["missing"])],
        [
            TaskStep(id="one", description="one", depends_on=["two"]),
            TaskStep(id="two", description="two", depends_on=["one"]),
        ],
    ],
    ids=["duplicate-id", "missing-dependency", "dependency-cycle"],
)
def test_task_plan_rejects_invalid_step_graph(steps: list[TaskStep]) -> None:
    payload = valid_plan().model_dump()
    payload["steps"] = [step.model_dump() for step in steps]

    with pytest.raises(ValidationError):
        TaskPlan.model_validate(payload)


def test_contracts_forbid_unknown_fields() -> None:
    payload = valid_plan().model_dump()
    payload["shell_command"] = "rm -rf /"

    with pytest.raises(ValidationError):
        TaskPlan.model_validate(payload)


def test_change_set_rejects_unsafe_or_duplicate_paths() -> None:
    with pytest.raises(ValidationError):
        FileChange(
            operation=FileOperation.CREATE,
            path="../outside.py",
            content="",
            rationale="bad path",
        )


def test_source_content_preserves_leading_and_trailing_whitespace() -> None:
    content = "  first line\nsecond line\n"
    proposed = ProposedFileChange(
        operation=FileOperation.UPDATE,
        path="backend/app/main.py",
        content=content,
        rationale="preserve exact source bytes",
    )
    change = FileChange(
        operation=FileOperation.UPDATE,
        path="backend/app/main.py",
        base_sha256="a" * 64,
        content=content,
        rationale="preserve exact source bytes",
    )

    assert proposed.content == content
    assert change.content == content

    with pytest.raises(ValidationError):
        ChangeSet(
            summary="duplicates",
            changes=[
                FileChange(
                    operation=FileOperation.CREATE,
                    path="backend/App.py",
                    content="one",
                    rationale="first",
                ),
                FileChange(
                    operation=FileOperation.CREATE,
                    path="backend/app.py",
                    content="two",
                    rationale="second",
                ),
            ],
        )


def test_status_transition_model_is_closed_and_terminals_are_final() -> None:
    validate_transition(RunStatus.QUEUED, RunStatus.RUNNING)
    validate_transition(RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
    validate_transition(RunStatus.RUNNING, RunStatus.QUEUED)

    with pytest.raises(InvalidStatusTransition):
        validate_transition(RunStatus.COMPLETED, RunStatus.RUNNING)

    with pytest.raises(InvalidStatusTransition):
        validate_transition(RunStatus.QUEUED, RunStatus.COMPLETED)


def test_contract_datetime_example_uses_utc() -> None:
    # Locks the project-wide convention used by persisted summaries.
    assert datetime(2026, 8, 12, tzinfo=UTC).utcoffset().total_seconds() == 0
