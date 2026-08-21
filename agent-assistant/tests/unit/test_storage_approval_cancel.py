from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.contracts import (
    ApprovalOutcome,
    InvalidStatusTransition,
    RiskLevel,
    RunStatus,
    TaskPlan,
    TaskStep,
)
from app.storage import (
    ApprovalConflictError,
    IdempotencyConflictError,
    LeaseLostError,
    SQLiteRunStore,
)


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteRunStore:
    return SQLiteRunStore(tmp_path / "runs.sqlite3", clock=FixedClock())


def plan() -> TaskPlan:
    return TaskPlan(
        summary="Add health endpoint",
        acceptance_criteria=["HTTP 200 when the database is healthy"],
        files_to_inspect=["backend/app/main.py"],
        allowed_change_globs=["backend/app/**/*.py"],
        steps=[TaskStep(id="T1", description="Implement endpoint")],
        risk_level=RiskLevel.LOW,
        test_profile="python-fastapi",
    )


def waiting_run(store: SQLiteRunStore):
    run = store.create_run(
        requirement="add health endpoint",
        project_id="student-management",
        project_profile_hash="a" * 64,
    )
    claim = store.claim_next_run("worker", lease_ttl=timedelta(seconds=30))
    assert claim is not None
    return store.save_plan_and_wait_for_approval(claim.token, plan())


def test_approval_is_hash_bound_persisted_and_requeues_the_run(
    store: SQLiteRunStore,
) -> None:
    waiting = waiting_run(store)

    approved = store.decide_plan(
        waiting.run_id,
        decision=ApprovalOutcome.APPROVE,
        plan_hash=waiting.plan_hash,
        project_profile_hash=waiting.project_profile_hash,
        idempotency_key="approval-1",
    )

    assert approved.status is RunStatus.QUEUED
    stored = store.get_approval(waiting.run_id)
    assert stored.decision is ApprovalOutcome.APPROVE
    assert stored.plan_hash == waiting.plan_hash
    assert stored.idempotency_key == "approval-1"


def test_repeated_identical_approval_is_idempotent_even_with_a_new_key(
    store: SQLiteRunStore,
) -> None:
    waiting = waiting_run(store)
    arguments = dict(
        decision=ApprovalOutcome.APPROVE,
        plan_hash=waiting.plan_hash,
        project_profile_hash=waiting.project_profile_hash,
    )
    first = store.decide_plan(waiting.run_id, idempotency_key="key-1", **arguments)
    event_count = len(store.list_events(waiting.run_id))

    same_key = store.decide_plan(waiting.run_id, idempotency_key="key-1", **arguments)
    new_key = store.decide_plan(waiting.run_id, idempotency_key="key-2", **arguments)

    assert same_key == first
    assert new_key == first
    assert len(store.list_events(waiting.run_id)) == event_count


def test_reused_idempotency_key_with_different_payload_is_rejected(
    store: SQLiteRunStore,
) -> None:
    waiting = waiting_run(store)
    store.decide_plan(
        waiting.run_id,
        decision=ApprovalOutcome.APPROVE,
        plan_hash=waiting.plan_hash,
        project_profile_hash=waiting.project_profile_hash,
        idempotency_key="same-key",
    )

    with pytest.raises(IdempotencyConflictError):
        store.decide_plan(
            waiting.run_id,
            decision=ApprovalOutcome.REJECT,
            plan_hash=waiting.plan_hash,
            project_profile_hash=waiting.project_profile_hash,
            idempotency_key="same-key",
        )


@pytest.mark.parametrize("stale_field", ["plan", "profile"])
def test_stale_approval_hash_is_rejected_without_changing_state(
    store: SQLiteRunStore, stale_field: str
) -> None:
    waiting = waiting_run(store)
    supplied_plan_hash = "b" * 64 if stale_field == "plan" else waiting.plan_hash
    supplied_profile_hash = (
        "c" * 64 if stale_field == "profile" else waiting.project_profile_hash
    )

    with pytest.raises(ApprovalConflictError):
        store.decide_plan(
            waiting.run_id,
            decision=ApprovalOutcome.APPROVE,
            plan_hash=supplied_plan_hash,
            project_profile_hash=supplied_profile_hash,
            idempotency_key=f"stale-{stale_field}",
        )

    assert store.get_run(waiting.run_id).status is RunStatus.WAITING_APPROVAL


def test_rejecting_a_plan_creates_an_irreversible_terminal_run(
    store: SQLiteRunStore,
) -> None:
    waiting = waiting_run(store)

    rejected = store.decide_plan(
        waiting.run_id,
        decision=ApprovalOutcome.REJECT,
        plan_hash=waiting.plan_hash,
        project_profile_hash=waiting.project_profile_hash,
        idempotency_key="reject-1",
        reason="scope is too broad",
    )

    assert rejected.status is RunStatus.REJECTED
    assert rejected.ended_at is not None
    assert rejected.terminal_reason == "scope is too broad"
    with pytest.raises(InvalidStatusTransition):
        store.request_cancel(rejected.run_id)


@pytest.mark.parametrize(
    "starting_status", [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL]
)
def test_cancel_request_is_persisted_and_idempotent(
    store: SQLiteRunStore, starting_status: RunStatus
) -> None:
    if starting_status is RunStatus.WAITING_APPROVAL:
        run = waiting_run(store)
        token = None
    else:
        run = store.create_run(
            requirement=f"cancel {starting_status.value}",
            project_id="student-management",
            project_profile_hash="d" * 64,
        )
        token = None
        if starting_status is RunStatus.RUNNING:
            claim = store.claim_next_run("worker", lease_ttl=timedelta(seconds=30))
            assert claim is not None
            token = claim.token
            run = claim.run

    cancelled = store.request_cancel(run.run_id)
    event_count = len(store.list_events(run.run_id))
    repeated = store.request_cancel(run.run_id)

    assert cancelled.status is RunStatus.CANCEL_REQUESTED
    assert repeated == cancelled
    assert len(store.list_events(run.run_id)) == event_count
    if token is not None:
        with pytest.raises(LeaseLostError):
            store.append_fenced_event(
                token,
                node="manager",
                event_type="late.write",
                summary="must stop after cancellation",
            )


def test_cancellation_reaper_commits_terminal_state_and_clears_lease(
    store: SQLiteRunStore,
) -> None:
    run = store.create_run(
        requirement="cancel running work",
        project_id="student-management",
        project_profile_hash="e" * 64,
    )
    claim = store.claim_next_run("worker", lease_ttl=timedelta(seconds=30))
    assert claim is not None
    store.request_cancel(run.run_id)

    cancelled = store.finalize_next_cancellation()

    assert cancelled is not None
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.ended_at is not None
    assert cancelled.worker_id is None
    assert cancelled.lease_expires_at is None
    assert store.finalize_next_cancellation() is None
    with pytest.raises(LeaseLostError):
        store.append_fenced_event(
            claim.token,
            node="developer",
            event_type="late.write",
            summary="must remain fenced",
        )


def test_cancellation_reason_survives_terminalization(store: SQLiteRunStore) -> None:
    run = store.create_run(
        requirement="cancel with an audit reason",
        project_id="student-management",
        project_profile_hash="f" * 64,
    )
    requested = store.request_cancel(run.run_id, reason="requirement withdrawn")
    cancelled = store.finalize_next_cancellation()

    assert requested.terminal_reason == "requirement withdrawn"
    assert cancelled is not None
    assert cancelled.terminal_reason == "requirement withdrawn"
