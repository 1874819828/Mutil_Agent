from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.contracts import RunStatus
from app.storage import LeaseLostError, SQLiteRunStore
from app.worker import LeaseCoordinator


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def create_run(store: SQLiteRunStore) -> str:
    return store.create_run(
        requirement="add health endpoint",
        project_id="student-management",
        project_profile_hash="f" * 64,
    ).run_id


def test_two_workers_cannot_claim_the_same_run(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "runs.sqlite3"
    create_run(SQLiteRunStore(database, clock=clock))

    def claim(worker_id: str):
        return SQLiteRunStore(database, clock=clock).claim_next_run(
            worker_id, lease_ttl=timedelta(seconds=30)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["worker-a", "worker-b"]))

    assert sum(item is not None for item in claims) == 1
    winner = next(item for item in claims if item is not None)
    assert winner.token.generation == 1


def test_expired_run_is_reclaimed_with_higher_fencing_generation(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3", clock=clock)
    run_id = create_run(store)
    old = store.claim_next_run("old", lease_ttl=timedelta(seconds=10))
    assert old is not None

    clock.advance(11)
    new = store.claim_next_run("new", lease_ttl=timedelta(seconds=10))

    assert new is not None
    assert new.run.run_id == run_id
    assert new.token.generation == old.token.generation + 1
    assert [event.event_type for event in store.list_events(run_id)] == [
        "run.created",
        "lease.claimed",
        "lease.expired",
        "lease.claimed",
    ]
    with pytest.raises(LeaseLostError):
        store.append_fenced_event(
            old.token,
            node="manager",
            event_type="stale.write",
            summary="must be rejected",
        )


def test_renewal_and_all_fenced_writes_require_current_unexpired_token(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3", clock=clock)
    create_run(store)
    claim = store.claim_next_run("worker-a", lease_ttl=timedelta(seconds=10))
    assert claim is not None

    renewed = store.renew_lease(claim.token, lease_ttl=timedelta(seconds=30))
    assert renewed.generation == claim.token.generation
    clock.advance(31)

    with pytest.raises(LeaseLostError):
        store.renew_lease(renewed, lease_ttl=timedelta(seconds=30))
    with pytest.raises(LeaseLostError):
        store.transition_fenced(renewed, RunStatus.FAILED, reason="too late")


def test_lease_coordinator_wraps_claim_renew_and_release(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3", clock=clock)
    create_run(store)
    coordinator = LeaseCoordinator(
        store,
        worker_id="worker-a",
        lease_ttl=timedelta(seconds=30),
    )

    claimed = coordinator.claim()
    assert claimed is not None
    renewed = coordinator.renew(claimed.token)
    released = coordinator.release_for_retry(renewed, reason="restart at checkpoint")

    assert released.status is RunStatus.QUEUED
    assert released.worker_id is None
    assert store.claim_next_run("worker-b", lease_ttl=timedelta(seconds=30)) is not None
