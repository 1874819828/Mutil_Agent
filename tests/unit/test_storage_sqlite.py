from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from app.contracts import RiskLevel, RunStatus, TaskPlan, TaskStep
from app.storage import ArtifactNotFoundError, SQLiteRunStore


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> SQLiteRunStore:
    return SQLiteRunStore(tmp_path / "runs.sqlite3", clock=clock)


def make_plan() -> TaskPlan:
    return TaskPlan(
        summary="Add health endpoint",
        acceptance_criteria=["GET /api/v1/health returns 200"],
        files_to_inspect=["backend/app/main.py"],
        allowed_change_globs=["backend/app/**/*.py"],
        steps=[TaskStep(id="T1", description="Implement endpoint")],
        risk_level=RiskLevel.LOW,
        test_profile="python-fastapi",
    )


def test_run_event_and_plan_survive_store_restart(
    tmp_path: Path, clock: MutableClock
) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database, clock=clock)
    run = first.create_run(
        requirement="add health endpoint",
        project_id="student-management",
        project_profile_hash="a" * 64,
    )
    claim = first.claim_next_run("worker-a", lease_ttl=timedelta(seconds=30))
    assert claim is not None
    waiting = first.save_plan_and_wait_for_approval(claim.token, make_plan())

    second = SQLiteRunStore(database, clock=clock)
    restored = second.get_run(run.run_id)

    assert restored.status is RunStatus.WAITING_APPROVAL
    assert restored.plan == make_plan()
    assert restored.plan_hash is not None
    assert restored.worker_id is None
    assert restored.lease_expires_at is None
    assert [event.event_type for event in second.list_events(run.run_id)] == [
        "run.created",
        "lease.claimed",
        "manager.plan_created",
    ]


def test_v5_migration_adds_git_publication_audit_columns(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE publications (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            project_profile_hash TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL,
            diff_sha256 TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            changed_files_json TEXT NOT NULL,
            backup_relative_path TEXT,
            published_manifest_hash TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            completed_at REAL
        )
        """
    )
    connection.commit()
    connection.close()

    SQLiteRunStore(database)

    connection = sqlite3.connect(database)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(publications)")
    }
    versions = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations")
    }
    connection.close()
    assert {
        "git_original_branch",
        "git_base_commit",
        "git_branch",
        "git_commit",
    } <= columns
    assert 5 in versions


def test_event_ids_are_ordered_and_run_tracks_last_event(store: SQLiteRunStore) -> None:
    run = store.create_run(
        requirement="work",
        project_id="project",
        project_profile_hash="b" * 64,
    )
    event = store.append_event(
        run.run_id,
        node="api",
        event_type="run.inspected",
        summary="inspected",
    )

    events = store.list_events(run.run_id)
    assert [item.event_id for item in events] == sorted(item.event_id for item in events)
    assert store.get_run(run.run_id).last_event_id == event.event_id


def test_artifacts_are_registered_by_opaque_id_and_paths_are_relative(
    store: SQLiteRunStore,
) -> None:
    run = store.create_run(
        requirement="work",
        project_id="project",
        project_profile_hash="c" * 64,
    )
    artifact = store.register_artifact(
        run_id=run.run_id,
        kind="manager-plan",
        relative_path="artifacts/manager-plan.json",
        sha256="d" * 64,
        size_bytes=120,
    )

    assert artifact.artifact_id != artifact.relative_path
    assert store.get_artifact(artifact.artifact_id) == artifact
    assert store.list_artifacts(run.run_id) == [artifact]
    with pytest.raises(ArtifactNotFoundError):
        store.get_artifact("artifacts/manager-plan.json")
    with pytest.raises(ValueError):
        store.register_artifact(
            run_id=run.run_id,
            kind="bad",
            relative_path="../secrets.env",
            sha256="e" * 64,
            size_bytes=1,
        )


def test_event_cannot_reference_an_artifact_from_another_run(
    store: SQLiteRunStore,
) -> None:
    first = store.create_run(
        requirement="first",
        project_id="project",
        project_profile_hash="1" * 64,
    )
    second = store.create_run(
        requirement="second",
        project_id="project",
        project_profile_hash="2" * 64,
    )
    artifact = store.register_artifact(
        run_id=first.run_id,
        kind="report",
        relative_path="artifacts/report.json",
        sha256="3" * 64,
        size_bytes=20,
    )

    with pytest.raises(ValueError, match="same run"):
        store.append_event(
            second.run_id,
            node="api",
            event_type="artifact.attached",
            summary="invalid cross-run link",
            artifact_id=artifact.artifact_id,
        )


def test_list_runs_returns_reverse_creation_order(store: SQLiteRunStore) -> None:
    first = store.create_run(
        requirement="first requirement",
        project_id="project",
        project_profile_hash="1" * 64,
    )
    second = store.create_run(
        requirement="second requirement",
        project_id="project",
        project_profile_hash="2" * 64,
    )

    listed = store.list_runs()
    assert [run.run_id for run in listed] == [second.run_id, first.run_id]
    assert all(
        run.requirement in ("first requirement", "second requirement") for run in listed
    )


def test_list_runs_honors_limit_and_offset(store: SQLiteRunStore) -> None:
    for index in range(5):
        store.create_run(
            requirement=f"requirement {index}",
            project_id="project",
            project_profile_hash=f"{index}" * 64,
        )

    page = store.list_runs(limit=2, offset=0)
    assert [run.requirement for run in page] == ["requirement 4", "requirement 3"]

    page = store.list_runs(limit=2, offset=2)
    assert [run.requirement for run in page] == ["requirement 2", "requirement 1"]

    with pytest.raises(ValueError, match="limit"):
        store.list_runs(limit=0)
    with pytest.raises(ValueError, match="limit"):
        store.list_runs(limit=201)
    with pytest.raises(ValueError, match="offset"):
        store.list_runs(offset=-1)
