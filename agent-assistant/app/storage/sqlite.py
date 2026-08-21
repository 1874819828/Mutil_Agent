"""Transactional SQLite storage for runs, events, artifacts, and worker leases."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from app.contracts import (
    ApprovalOutcome,
    RunStatus,
    TERMINAL_STATUSES,
    TaskPlan,
    canonical_json_hash,
    validate_transition,
)

from .errors import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ArtifactNotFoundError,
    IdempotencyConflictError,
    LeaseLostError,
    PublicationConflictError,
    RunNotFoundError,
)
from .records import (
    ApprovalRecord,
    ArtifactRecord,
    ClaimedRun,
    EventRecord,
    LeaseToken,
    LLMCallRecord,
    PublicationRecord,
    RunRecord,
)


SCHEMA_VERSION = 5
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.timestamp()


def _datetime(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _require_text(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be blank")
    return value


def _artifact_path(value: str) -> str:
    value = value.strip()
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or ":" in value
        or path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("artifact path must be a canonical portable relative path")
    return path.as_posix()


class SQLiteRunStore:
    """A small synchronous repository with one connection per operation.

    Write transactions use ``BEGIN IMMEDIATE`` so claiming a run is atomic even
    when multiple worker threads or processes contend for the same SQLite file.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self._timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    parent_run_id TEXT REFERENCES runs(run_id),
                    project_id TEXT NOT NULL,
                    project_profile_hash TEXT NOT NULL,
                    requirement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_node TEXT,
                    plan_json TEXT,
                    plan_hash TEXT,
                    worker_id TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at REAL,
                    heartbeat_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    ended_at REAL,
                    terminal_reason TEXT,
                    last_event_id INTEGER,
                    source_manifest_hash TEXT,
                    context_bundle_hash TEXT,
                    acceptance_pack_id TEXT,
                    acceptance_pack_hash TEXT,
                    provider_mode TEXT NOT NULL DEFAULT 'mock'
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    created_at REAL NOT NULL,
                    UNIQUE (run_id, relative_path)
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    node TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    artifact_id TEXT REFERENCES artifacts(artifact_id),
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                    decision TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    project_profile_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    reason TEXT,
                    created_at REAL NOT NULL,
                    source_manifest_hash TEXT,
                    context_bundle_hash TEXT,
                    acceptance_pack_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS llm_calls (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    lease_generation INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    configured_model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reserved_input_tokens INTEGER NOT NULL,
                    reserved_output_tokens INTEGER NOT NULL,
                    actual_model TEXT,
                    response_id TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    error_type TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );

                CREATE TABLE IF NOT EXISTS approval_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    request_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publications (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    project_profile_hash TEXT NOT NULL,
                    source_manifest_hash TEXT NOT NULL,
                    diff_sha256 TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    changed_files_json TEXT NOT NULL,
                    backup_relative_path TEXT,
                    published_manifest_hash TEXT,
                    git_original_branch TEXT,
                    git_base_commit TEXT,
                    git_branch TEXT,
                    git_commit TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );

                CREATE TABLE IF NOT EXISTS publication_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    request_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_claim
                    ON runs(status, lease_expires_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_run
                    ON events(run_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run
                    ON artifacts(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_run
                    ON llm_calls(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_publications_status
                    ON publications(status, created_at);
                """
            )
            self._migrate_v3_columns(connection)
            self._migrate_v5_columns(connection)
            now = _timestamp(self._clock())
            for version in range(1, SCHEMA_VERSION + 1):
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, now),
                )

    @staticmethod
    def _migrate_v3_columns(connection: sqlite3.Connection) -> None:
        """Add v3 columns to databases created by the MVD without data loss."""

        run_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        for name, definition in (
            ("source_manifest_hash", "TEXT"),
            ("context_bundle_hash", "TEXT"),
            ("acceptance_pack_id", "TEXT"),
            ("acceptance_pack_hash", "TEXT"),
            ("provider_mode", "TEXT NOT NULL DEFAULT 'mock'"),
        ):
            if name not in run_columns:
                connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
        approval_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        for name in (
            "source_manifest_hash",
            "context_bundle_hash",
            "acceptance_pack_hash",
        ):
            if name not in approval_columns:
                connection.execute(f"ALTER TABLE approvals ADD COLUMN {name} TEXT")

    @staticmethod
    def _migrate_v5_columns(connection: sqlite3.Connection) -> None:
        """Add Git delivery audit columns without invalidating older publications."""

        publication_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(publications)").fetchall()
        }
        for name in (
            "git_original_branch",
            "git_base_commit",
            "git_branch",
            "git_commit",
        ):
            if name not in publication_columns:
                connection.execute(
                    f"ALTER TABLE publications ADD COLUMN {name} TEXT"
                )

    def create_run(
        self,
        *,
        requirement: str,
        project_id: str,
        project_profile_hash: str,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        source_manifest_hash: str | None = None,
        acceptance_pack_id: str | None = None,
        acceptance_pack_hash: str | None = None,
        provider_mode: str = "mock",
    ) -> RunRecord:
        requirement = _require_text(requirement, "requirement")
        project_id = _require_text(project_id, "project_id")
        if not _SHA256.fullmatch(project_profile_hash):
            raise ValueError("project_profile_hash must be a lowercase SHA-256 digest")
        if source_manifest_hash is not None and not _SHA256.fullmatch(source_manifest_hash):
            raise ValueError("source_manifest_hash must be a lowercase SHA-256 digest")
        if acceptance_pack_hash is not None and not _SHA256.fullmatch(acceptance_pack_hash):
            raise ValueError("acceptance_pack_hash must be a lowercase SHA-256 digest")
        provider_mode = _require_text(provider_mode, "provider_mode")
        run_id = run_id or str(uuid4())
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, parent_run_id, project_id, project_profile_hash,
                    requirement, status, current_node, created_at, updated_at,
                    source_manifest_hash, acceptance_pack_id, acceptance_pack_hash,
                    provider_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    parent_run_id,
                    project_id,
                    project_profile_hash,
                    requirement,
                    RunStatus.QUEUED.value,
                    "validate_request",
                    now,
                    now,
                    source_manifest_hash,
                    acceptance_pack_id,
                    acceptance_pack_hash,
                    provider_mode,
                ),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                node="api",
                event_type="run.created",
                summary="Run queued",
                artifact_id=None,
                created_at=now,
            )
            return self._get_run(connection, run_id)

    def bind_source_manifest(self, run_id: str, source_manifest_hash: str) -> RunRecord:
        """Bind the immutable source snapshot before a worker claims the run."""

        if not _SHA256.fullmatch(source_manifest_hash):
            raise ValueError("source_manifest_hash must be a lowercase SHA-256 digest")
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(run_id)
            if row["status"] != RunStatus.QUEUED.value or int(row["lease_generation"]) != 0:
                raise ValueError("source manifest can only be bound before the first claim")
            prior = row["source_manifest_hash"]
            if prior is not None and prior != source_manifest_hash:
                raise ValueError("source manifest is already bound to another digest")
            connection.execute(
                "UPDATE runs SET source_manifest_hash = ?, updated_at = ? WHERE run_id = ?",
                (source_manifest_hash, now, run_id),
            )
            return self._get_run(connection, run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self._read_connection() as connection:
            return self._get_run(connection, run_id)

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        """List runs in reverse creation order for the console overview."""
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._run_record(row) for row in rows]

    def _get_run(self, connection: sqlite3.Connection, run_id: str) -> RunRecord:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return self._run_record(row)

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        plan = TaskPlan.model_validate_json(row["plan_json"]) if row["plan_json"] else None
        return RunRecord(
            run_id=row["run_id"],
            parent_run_id=row["parent_run_id"],
            project_id=row["project_id"],
            project_profile_hash=row["project_profile_hash"],
            requirement=row["requirement"],
            status=RunStatus(row["status"]),
            current_node=row["current_node"],
            plan=plan,
            plan_hash=row["plan_hash"],
            worker_id=row["worker_id"],
            lease_generation=row["lease_generation"],
            lease_expires_at=_datetime(row["lease_expires_at"]),
            heartbeat_at=_datetime(row["heartbeat_at"]),
            created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_datetime(row["updated_at"]),  # type: ignore[arg-type]
            started_at=_datetime(row["started_at"]),
            ended_at=_datetime(row["ended_at"]),
            terminal_reason=row["terminal_reason"],
            last_event_id=row["last_event_id"],
            source_manifest_hash=row["source_manifest_hash"],
            context_bundle_hash=row["context_bundle_hash"],
            acceptance_pack_id=row["acceptance_pack_id"],
            acceptance_pack_hash=row["acceptance_pack_hash"],
            provider_mode=row["provider_mode"],
        )

    def append_event(
        self,
        run_id: str,
        *,
        node: str,
        event_type: str,
        summary: str,
        artifact_id: str | None = None,
    ) -> EventRecord:
        """Append an API/system event outside worker execution.

        Worker-owned events must use :meth:`append_fenced_event`.
        """

        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            self._get_run(connection, run_id)
            return self._insert_event(
                connection,
                run_id=run_id,
                node=node,
                event_type=event_type,
                summary=summary,
                artifact_id=artifact_id,
                created_at=now,
            )

    def append_fenced_event(
        self,
        token: LeaseToken,
        *,
        node: str,
        event_type: str,
        summary: str,
        artifact_id: str | None = None,
    ) -> EventRecord:
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            self._require_active_lease(connection, token, now)
            return self._insert_event(
                connection,
                run_id=token.run_id,
                node=node,
                event_type=event_type,
                summary=summary,
                artifact_id=artifact_id,
                created_at=now,
            )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        node: str,
        event_type: str,
        summary: str,
        artifact_id: str | None,
        created_at: float,
    ) -> EventRecord:
        if artifact_id is not None:
            artifact = connection.execute(
                "SELECT run_id FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if artifact is None or artifact["run_id"] != run_id:
                raise ValueError("an event artifact must exist and belong to the same run")
        cursor = connection.execute(
            """
            INSERT INTO events(run_id, node, event_type, summary, artifact_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _require_text(node, "node"),
                _require_text(event_type, "event_type"),
                _require_text(summary, "summary"),
                artifact_id,
                created_at,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an event id")
        event_id = cursor.lastrowid
        connection.execute(
            "UPDATE runs SET last_event_id = ?, updated_at = ? WHERE run_id = ?",
            (event_id, created_at, run_id),
        )
        return EventRecord(
            event_id=event_id,
            run_id=run_id,
            node=node.strip(),
            event_type=event_type.strip(),
            summary=summary.strip(),
            artifact_id=artifact_id,
            created_at=_datetime(created_at),  # type: ignore[arg-type]
        )

    def list_events(self, run_id: str, *, after_event_id: int = 0) -> list[EventRecord]:
        with self._read_connection() as connection:
            self._get_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND event_id > ?
                ORDER BY event_id ASC
                """,
                (run_id, after_event_id),
            ).fetchall()
        return [self._event_record(row) for row in rows]

    @staticmethod
    def _event_record(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            event_id=row["event_id"],
            run_id=row["run_id"],
            node=row["node"],
            event_type=row["event_type"],
            summary=row["summary"],
            artifact_id=row["artifact_id"],
            created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
        )

    def register_artifact(
        self,
        *,
        run_id: str,
        kind: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        """Register a non-worker artifact (worker code uses the fenced variant)."""

        with self._transaction(immediate=True) as connection:
            self._get_run(connection, run_id)
            return self._insert_artifact(
                connection,
                run_id=run_id,
                kind=kind,
                relative_path=relative_path,
                sha256=sha256,
                size_bytes=size_bytes,
                artifact_id=artifact_id,
                created_at=_timestamp(self._clock()),
            )

    def register_artifact_fenced(
        self,
        token: LeaseToken,
        *,
        kind: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            self._require_active_lease(connection, token, now)
            return self._insert_artifact(
                connection,
                run_id=token.run_id,
                kind=kind,
                relative_path=relative_path,
                sha256=sha256,
                size_bytes=size_bytes,
                artifact_id=artifact_id,
                created_at=now,
            )

    def _insert_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
        artifact_id: str | None,
        created_at: float,
    ) -> ArtifactRecord:
        relative_path = _artifact_path(relative_path)
        if not _SHA256.fullmatch(sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        artifact_id = artifact_id or str(uuid4())
        kind = _require_text(kind, "kind")
        connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, run_id, kind, relative_path, sha256, size_bytes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, run_id, kind, relative_path, sha256, size_bytes, created_at),
        )
        return ArtifactRecord(
            artifact_id=artifact_id,
            run_id=run_id,
            kind=kind,
            relative_path=relative_path,
            sha256=sha256,
            size_bytes=size_bytes,
            created_at=_datetime(created_at),  # type: ignore[arg-type]
        )

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(artifact_id)
        return self._artifact_record(row)

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        """List registered artifacts for one run without exposing filesystem paths as IDs."""

        with self._read_connection() as connection:
            self._get_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE run_id = ?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (run_id,),
            ).fetchall()
            return [self._artifact_record(row) for row in rows]

    def get_publication(self, run_id: str) -> PublicationRecord | None:
        with self._read_connection() as connection:
            self._get_run(connection, run_id)
            row = connection.execute(
                "SELECT * FROM publications WHERE run_id = ?", (run_id,)
            ).fetchone()
            return None if row is None else self._publication_record(row)

    def list_incomplete_publications(self) -> list[PublicationRecord]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM publications WHERE status = 'publishing' "
                "ORDER BY created_at ASC"
            ).fetchall()
            return [self._publication_record(row) for row in rows]

    def begin_publication(
        self,
        *,
        run_id: str,
        project_profile_hash: str,
        source_manifest_hash: str,
        diff_sha256: str,
        request_hash: str,
        idempotency_key: str,
        changed_files: tuple[str, ...],
        backup_relative_path: str,
        git_original_branch: str,
        git_base_commit: str,
        git_branch: str,
    ) -> PublicationRecord:
        """Start one publication attempt with durable idempotency semantics."""

        for field, value in (
            ("project_profile_hash", project_profile_hash),
            ("source_manifest_hash", source_manifest_hash),
            ("diff_sha256", diff_sha256),
            ("request_hash", request_hash),
        ):
            if not _SHA256.fullmatch(value):
                raise ValueError(f"{field} must be a lowercase SHA-256 digest")
        idempotency_key = _require_text(idempotency_key, "idempotency_key")
        normalized_files = tuple(_artifact_path(path) for path in changed_files)
        if not normalized_files or len(set(normalized_files)) != len(normalized_files):
            raise ValueError("changed_files must be non-empty and unique")
        backup_relative_path = _artifact_path(backup_relative_path)
        git_original_branch = _require_text(git_original_branch, "git_original_branch")
        git_branch = _require_text(git_branch, "git_branch")
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", git_base_commit):
            raise ValueError("git_base_commit must be a supported Git object id")
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            run = self._get_run(connection, run_id)
            if run.status is not RunStatus.COMPLETED:
                raise PublicationConflictError(
                    "only a completed run can be published"
                )
            prior_key = connection.execute(
                "SELECT * FROM publication_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            current_row = connection.execute(
                "SELECT * FROM publications WHERE run_id = ?", (run_id,)
            ).fetchone()
            if prior_key is not None:
                if (
                    prior_key["run_id"] != run_id
                    or prior_key["request_hash"] != request_hash
                ):
                    raise IdempotencyConflictError(idempotency_key)
                if (
                    current_row is not None
                    and current_row["request_hash"] == request_hash
                    and current_row["idempotency_key"] == idempotency_key
                ):
                    return self._publication_record(current_row)
                raise PublicationConflictError(
                    "idempotent publication attempt has been superseded"
                )
            if current_row is not None and current_row["status"] in {
                "publishing",
                "published",
            }:
                raise PublicationConflictError(
                    f"run publication is already {current_row['status']}"
                )
            connection.execute(
                "INSERT INTO publication_idempotency"
                "(idempotency_key, run_id, request_hash, created_at) VALUES (?, ?, ?, ?)",
                (idempotency_key, run_id, request_hash, now),
            )
            connection.execute(
                """
                INSERT INTO publications(
                    run_id, status, project_profile_hash, source_manifest_hash,
                    diff_sha256, request_hash, idempotency_key, changed_files_json,
                    backup_relative_path, published_manifest_hash,
                    git_original_branch, git_base_commit, git_branch, git_commit, error,
                    created_at, completed_at
                ) VALUES (?, 'publishing', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, ?, NULL)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = 'publishing',
                    project_profile_hash = excluded.project_profile_hash,
                    source_manifest_hash = excluded.source_manifest_hash,
                    diff_sha256 = excluded.diff_sha256,
                    request_hash = excluded.request_hash,
                    idempotency_key = excluded.idempotency_key,
                    changed_files_json = excluded.changed_files_json,
                    backup_relative_path = excluded.backup_relative_path,
                    published_manifest_hash = NULL,
                    git_original_branch = excluded.git_original_branch,
                    git_base_commit = excluded.git_base_commit,
                    git_branch = excluded.git_branch,
                    git_commit = NULL,
                    error = NULL,
                    created_at = excluded.created_at,
                    completed_at = NULL
                """,
                (
                    run_id,
                    project_profile_hash,
                    source_manifest_hash,
                    diff_sha256,
                    request_hash,
                    idempotency_key,
                    json.dumps(normalized_files, ensure_ascii=True),
                    backup_relative_path,
                    git_original_branch,
                    git_base_commit,
                    git_branch,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM publications WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert row is not None
            return self._publication_record(row)

    def complete_publication(
        self,
        run_id: str,
        *,
        request_hash: str,
        published_manifest_hash: str,
        git_commit: str,
    ) -> PublicationRecord:
        if not _SHA256.fullmatch(published_manifest_hash):
            raise ValueError("published_manifest_hash must be a lowercase SHA-256 digest")
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", git_commit):
            raise ValueError("git_commit must be a supported Git object id")
        return self._finish_publication(
            run_id,
            request_hash=request_hash,
            status="published",
            published_manifest_hash=published_manifest_hash,
            git_commit=git_commit,
            error=None,
        )

    def fail_publication(
        self,
        run_id: str,
        *,
        request_hash: str,
        error: str,
        manual_recovery_required: bool = False,
    ) -> PublicationRecord:
        error = " ".join(error.split())[:1_000] or "publication failed"
        return self._finish_publication(
            run_id,
            request_hash=request_hash,
            status=("manual_recovery_required" if manual_recovery_required else "failed"),
            published_manifest_hash=None,
            git_commit=None,
            error=error,
        )

    def _finish_publication(
        self,
        run_id: str,
        *,
        request_hash: str,
        status: str,
        published_manifest_hash: str | None,
        git_commit: str | None,
        error: str | None,
    ) -> PublicationRecord:
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "publishing"
                or row["request_hash"] != request_hash
            ):
                raise PublicationConflictError("publication attempt is no longer active")
            connection.execute(
                """
                UPDATE publications
                SET status = ?, published_manifest_hash = ?, git_commit = ?,
                    error = ?, completed_at = ?
                WHERE run_id = ? AND status = 'publishing' AND request_hash = ?
                """,
                (
                    status,
                    published_manifest_hash,
                    git_commit,
                    error,
                    now,
                    run_id,
                    request_hash,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM publications WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated is not None
            return self._publication_record(updated)

    @staticmethod
    def _publication_record(row: sqlite3.Row) -> PublicationRecord:
        changed = json.loads(row["changed_files_json"])
        if not isinstance(changed, list) or not all(
            isinstance(item, str) for item in changed
        ):
            raise RuntimeError("stored publication changed_files_json is invalid")
        return PublicationRecord(
            run_id=row["run_id"],
            status=row["status"],
            project_profile_hash=row["project_profile_hash"],
            source_manifest_hash=row["source_manifest_hash"],
            diff_sha256=row["diff_sha256"],
            request_hash=row["request_hash"],
            idempotency_key=row["idempotency_key"],
            changed_files=tuple(changed),
            backup_relative_path=row["backup_relative_path"],
            published_manifest_hash=row["published_manifest_hash"],
            git_original_branch=row["git_original_branch"],
            git_base_commit=row["git_base_commit"],
            git_branch=row["git_branch"],
            git_commit=row["git_commit"],
            error=row["error"],
            created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
            completed_at=_datetime(row["completed_at"]),
        )

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            relative_path=row["relative_path"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
        )

    def claim_next_run(self, worker_id: str, *, lease_ttl: timedelta) -> ClaimedRun | None:
        worker_id = _require_text(worker_id, "worker_id")
        ttl_seconds = lease_ttl.total_seconds()
        if ttl_seconds <= 0:
            raise ValueError("lease_ttl must be positive")
        now = _timestamp(self._clock())
        expires_at = now + ttl_seconds
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status = ?
                   OR (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                ORDER BY created_at ASC, run_id ASC
                LIMIT 1
                """,
                (RunStatus.QUEUED.value, RunStatus.RUNNING.value, now),
            ).fetchone()
            if row is None:
                return None
            previous_status = RunStatus(row["status"])
            if previous_status is RunStatus.RUNNING:
                validate_transition(RunStatus.RUNNING, RunStatus.QUEUED)
                connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, worker_id = NULL, lease_expires_at = NULL,
                        heartbeat_at = NULL, updated_at = ?
                    WHERE run_id = ? AND status = ? AND lease_generation = ?
                      AND lease_expires_at <= ?
                    """,
                    (
                        RunStatus.QUEUED.value,
                        now,
                        row["run_id"],
                        RunStatus.RUNNING.value,
                        row["lease_generation"],
                        now,
                    ),
                )
                self._insert_event(
                    connection,
                    run_id=row["run_id"],
                    node="worker",
                    event_type="lease.expired",
                    summary="Expired lease returned to the queue",
                    artifact_id=None,
                    created_at=now,
                )
            validate_transition(RunStatus.QUEUED, RunStatus.RUNNING)
            generation = int(row["lease_generation"]) + 1
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, worker_id = ?, lease_generation = ?,
                    lease_expires_at = ?, heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE run_id = ? AND lease_generation = ?
                  AND status = ?
                """,
                (
                    RunStatus.RUNNING.value,
                    worker_id,
                    generation,
                    expires_at,
                    now,
                    now,
                    now,
                    row["run_id"],
                    row["lease_generation"],
                    RunStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            event_summary = (
                "Expired lease reclaimed" if previous_status is RunStatus.RUNNING else "Run claimed"
            )
            self._insert_event(
                connection,
                run_id=row["run_id"],
                node="worker",
                event_type="lease.claimed",
                summary=event_summary,
                artifact_id=None,
                created_at=now,
            )
            run = self._get_run(connection, row["run_id"])
            token = LeaseToken(
                run_id=run.run_id,
                worker_id=worker_id,
                generation=generation,
                expires_at=_datetime(expires_at),  # type: ignore[arg-type]
            )
            return ClaimedRun(run=run, token=token)

    def renew_lease(self, token: LeaseToken, *, lease_ttl: timedelta) -> LeaseToken:
        ttl_seconds = lease_ttl.total_seconds()
        if ttl_seconds <= 0:
            raise ValueError("lease_ttl must be positive")
        now = _timestamp(self._clock())
        expires_at = now + ttl_seconds
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE run_id = ? AND worker_id = ? AND lease_generation = ?
                  AND status = ? AND lease_expires_at > ?
                """,
                (
                    expires_at,
                    now,
                    now,
                    token.run_id,
                    token.worker_id,
                    token.generation,
                    RunStatus.RUNNING.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(token.run_id, token.worker_id, token.generation)
        return LeaseToken(
            run_id=token.run_id,
            worker_id=token.worker_id,
            generation=token.generation,
            expires_at=_datetime(expires_at),  # type: ignore[arg-type]
        )

    def assert_active_lease(self, token: LeaseToken) -> None:
        now = _timestamp(self._clock())
        with self._read_connection() as connection:
            self._require_active_lease(connection, token, now)

    @staticmethod
    def _require_active_lease(
        connection: sqlite3.Connection, token: LeaseToken, now: float
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM runs
            WHERE run_id = ? AND worker_id = ? AND lease_generation = ?
              AND status = ? AND lease_expires_at > ?
            """,
            (
                token.run_id,
                token.worker_id,
                token.generation,
                RunStatus.RUNNING.value,
                now,
            ),
        ).fetchone()
        if row is None:
            raise LeaseLostError(token.run_id, token.worker_id, token.generation)
        return row

    def save_plan_and_wait_for_approval(
        self,
        token: LeaseToken,
        plan: TaskPlan,
        *,
        source_manifest_hash: str | None = None,
        context_bundle_hash: str | None = None,
        acceptance_pack_hash: str | None = None,
    ) -> RunRecord:
        now = _timestamp(self._clock())
        plan_json = json.dumps(
            plan.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        plan_hash = canonical_json_hash(plan)
        for field, digest in (
            ("source_manifest_hash", source_manifest_hash),
            ("context_bundle_hash", context_bundle_hash),
            ("acceptance_pack_hash", acceptance_pack_hash),
        ):
            if digest is not None and not _SHA256.fullmatch(digest):
                raise ValueError(f"{field} must be a lowercase SHA-256 digest")
        with self._transaction(immediate=True) as connection:
            row = self._require_active_lease(connection, token, now)
            expected_source = source_manifest_hash or row["source_manifest_hash"]
            expected_pack = acceptance_pack_hash or row["acceptance_pack_hash"]
            if row["source_manifest_hash"] and row["source_manifest_hash"] != expected_source:
                raise ValueError("source manifest changed before plan publication")
            if row["acceptance_pack_hash"] and row["acceptance_pack_hash"] != expected_pack:
                raise ValueError("acceptance pack changed before plan publication")
            validate_transition(RunStatus(row["status"]), RunStatus.WAITING_APPROVAL)
            cursor = connection.execute(
                """
                UPDATE runs
                SET plan_json = ?, plan_hash = ?, status = ?, current_node = ?,
                    source_manifest_hash = ?, context_bundle_hash = ?,
                    acceptance_pack_hash = ?,
                    worker_id = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE run_id = ? AND worker_id = ? AND lease_generation = ?
                  AND status = ? AND lease_expires_at > ?
                """,
                (
                    plan_json,
                    plan_hash,
                    RunStatus.WAITING_APPROVAL.value,
                    "wait_for_plan_approval",
                    expected_source,
                    context_bundle_hash,
                    expected_pack,
                    now,
                    token.run_id,
                    token.worker_id,
                    token.generation,
                    RunStatus.RUNNING.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(token.run_id, token.worker_id, token.generation)
            self._insert_event(
                connection,
                run_id=token.run_id,
                node="manager",
                event_type="manager.plan_created",
                summary="Manager plan persisted; waiting for approval",
                artifact_id=None,
                created_at=now,
            )
            return self._get_run(connection, token.run_id)

    def decide_plan(
        self,
        run_id: str,
        *,
        decision: ApprovalOutcome,
        plan_hash: str | None,
        project_profile_hash: str,
        source_manifest_hash: str | None = None,
        context_bundle_hash: str | None = None,
        acceptance_pack_hash: str | None = None,
        idempotency_key: str,
        reason: str | None = None,
    ) -> RunRecord:
        """Persist one hash-bound approval decision and apply its state change."""

        decision = ApprovalOutcome(decision)
        idempotency_key = _require_text(idempotency_key, "idempotency_key")
        if not plan_hash or not _SHA256.fullmatch(plan_hash):
            raise ApprovalConflictError("approval plan_hash is missing or invalid")
        if not _SHA256.fullmatch(project_profile_hash):
            raise ApprovalConflictError("approval project_profile_hash is invalid")
        for field, digest in (
            ("source_manifest_hash", source_manifest_hash),
            ("context_bundle_hash", context_bundle_hash),
            ("acceptance_pack_hash", acceptance_pack_hash),
        ):
            if digest is not None and not _SHA256.fullmatch(digest):
                raise ApprovalConflictError(f"approval {field} is invalid")
        request_hash = canonical_json_hash(
            {
                "run_id": run_id,
                "decision": decision.value,
                "plan_hash": plan_hash,
                "project_profile_hash": project_profile_hash,
                "source_manifest_hash": source_manifest_hash,
                "context_bundle_hash": context_bundle_hash,
                "acceptance_pack_hash": acceptance_pack_hash,
                "reason": reason,
            }
        )
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            prior_request = connection.execute(
                "SELECT * FROM approval_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior_request is not None:
                if prior_request["request_hash"] != request_hash:
                    raise IdempotencyConflictError(idempotency_key)
                return self._get_run(connection, run_id)

            run = self._get_run(connection, run_id)
            if (
                run.plan_hash != plan_hash
                or run.project_profile_hash != project_profile_hash
                or (source_manifest_hash is not None and run.source_manifest_hash != source_manifest_hash)
                or (context_bundle_hash is not None and run.context_bundle_hash != context_bundle_hash)
                or (acceptance_pack_hash is not None and run.acceptance_pack_hash != acceptance_pack_hash)
            ):
                raise ApprovalConflictError(
                    "approval hashes do not match the persisted plan and project profile"
                )

            prior_approval = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ?", (run_id,)
            ).fetchone()
            if prior_approval is not None:
                same_decision = (
                    prior_approval["decision"] == decision.value
                    and prior_approval["plan_hash"] == plan_hash
                    and prior_approval["project_profile_hash"] == project_profile_hash
                    and prior_approval["source_manifest_hash"] == source_manifest_hash
                    and prior_approval["context_bundle_hash"] == context_bundle_hash
                    and prior_approval["acceptance_pack_hash"] == acceptance_pack_hash
                    and prior_approval["reason"] == reason
                )
                if not same_decision:
                    raise ApprovalConflictError("a different approval already exists for this run")
                connection.execute(
                    """
                    INSERT INTO approval_idempotency(
                        idempotency_key, run_id, request_hash, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (idempotency_key, run_id, request_hash, now),
                )
                return run

            target = (
                RunStatus.QUEUED
                if decision is ApprovalOutcome.APPROVE
                else RunStatus.REJECTED
            )
            validate_transition(run.status, target)
            terminal_reason = (
                (reason or "Plan rejected by user")
                if decision is ApprovalOutcome.REJECT
                else None
            )
            connection.execute(
                """
                INSERT INTO approvals(
                    run_id, decision, plan_hash, project_profile_hash,
                    idempotency_key, reason, created_at, source_manifest_hash,
                    context_bundle_hash, acceptance_pack_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    decision.value,
                    plan_hash,
                    project_profile_hash,
                    idempotency_key,
                    reason,
                    now,
                    source_manifest_hash,
                    context_bundle_hash,
                    acceptance_pack_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO approval_idempotency(
                    idempotency_key, run_id, request_hash, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (idempotency_key, run_id, request_hash, now),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, terminal_reason = ?, ended_at = ?, updated_at = ?
                WHERE run_id = ? AND status = ? AND plan_hash = ?
                  AND project_profile_hash = ?
                """,
                (
                    target.value,
                    terminal_reason,
                    now if target is RunStatus.REJECTED else None,
                    now,
                    run_id,
                    RunStatus.WAITING_APPROVAL.value,
                    plan_hash,
                    project_profile_hash,
                ),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                node="api",
                event_type=(
                    "approval.approved"
                    if decision is ApprovalOutcome.APPROVE
                    else "approval.rejected"
                ),
                summary=(
                    "Plan approved; run returned to queue"
                    if decision is ApprovalOutcome.APPROVE
                    else "Plan rejected"
                ),
                artifact_id=None,
                created_at=now,
            )
            return self._get_run(connection, run_id)

    def get_approval(self, run_id: str) -> ApprovalRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(run_id)
        return ApprovalRecord(
            run_id=row["run_id"],
            decision=ApprovalOutcome(row["decision"]),
            plan_hash=row["plan_hash"],
            project_profile_hash=row["project_profile_hash"],
            idempotency_key=row["idempotency_key"],
            reason=row["reason"],
            created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
            source_manifest_hash=row["source_manifest_hash"],
            context_bundle_hash=row["context_bundle_hash"],
            acceptance_pack_hash=row["acceptance_pack_hash"],
        )

    def reserve_llm_call(
        self,
        token: LeaseToken,
        *,
        call_id: str,
        role: str,
        prompt_version: str,
        request_hash: str,
        configured_model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        max_calls_per_run: int,
        max_input_tokens_per_run: int,
        max_output_tokens_per_run: int,
    ) -> LLMCallRecord:
        """Atomically reserve one call budget under the active generation."""

        call_id = _require_text(call_id, "call_id")
        role = _require_text(role, "role")
        prompt_version = _require_text(prompt_version, "prompt_version")
        configured_model = _require_text(configured_model, "configured_model")
        if not _SHA256.fullmatch(request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        if reserved_input_tokens < 0 or reserved_output_tokens < 1:
            raise ValueError("reserved token budgets are invalid")
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            self._require_active_lease(connection, token, now)
            used = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(
                           CASE
                               WHEN input_tokens IS NOT NULL
                                    AND input_tokens > reserved_input_tokens
                               THEN input_tokens ELSE reserved_input_tokens
                           END
                       ), 0) AS input_tokens,
                       COALESCE(SUM(
                           CASE
                               WHEN output_tokens IS NOT NULL
                                    AND output_tokens > reserved_output_tokens
                               THEN output_tokens ELSE reserved_output_tokens
                           END
                       ), 0) AS output_tokens
                FROM llm_calls WHERE run_id = ?
                """,
                (token.run_id,),
            ).fetchone()
            if int(used["calls"]) + 1 > max_calls_per_run:
                raise ValueError("LLM call budget exhausted")
            if int(used["input_tokens"]) + reserved_input_tokens > max_input_tokens_per_run:
                raise ValueError("LLM input token budget exhausted")
            if int(used["output_tokens"]) + reserved_output_tokens > max_output_tokens_per_run:
                raise ValueError("LLM output token budget exhausted")
            connection.execute(
                """
                INSERT INTO llm_calls(
                    call_id, run_id, lease_generation, role, prompt_version,
                    request_hash, configured_model, status,
                    reserved_input_tokens, reserved_output_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (
                    call_id,
                    token.run_id,
                    token.generation,
                    role,
                    prompt_version,
                    request_hash,
                    configured_model,
                    reserved_input_tokens,
                    reserved_output_tokens,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM llm_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            return self._llm_call_record(row)

    def finalize_llm_call(
        self,
        token: LeaseToken,
        *,
        call_id: str,
        status: str,
        actual_model: str | None = None,
        response_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_type: str | None = None,
    ) -> LLMCallRecord:
        """Finalize metadata only if the reserving generation still owns the run."""

        status = _require_text(status, "status")
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            self._require_active_lease(connection, token, now)
            cursor = connection.execute(
                """
                UPDATE llm_calls
                SET status = ?, actual_model = ?, response_id = ?, input_tokens = ?,
                    output_tokens = ?, error_type = ?, completed_at = ?
                WHERE call_id = ? AND run_id = ? AND lease_generation = ?
                  AND status = 'reserved'
                """,
                (
                    status,
                    actual_model,
                    response_id,
                    input_tokens,
                    output_tokens,
                    error_type,
                    now,
                    call_id,
                    token.run_id,
                    token.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(token.run_id, token.worker_id, token.generation)
            row = connection.execute(
                "SELECT * FROM llm_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            return self._llm_call_record(row)

    def list_llm_calls(self, run_id: str) -> list[LLMCallRecord]:
        with self._read_connection() as connection:
            self._get_run(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY created_at, call_id",
                (run_id,),
            ).fetchall()
            return [self._llm_call_record(row) for row in rows]

    @staticmethod
    def _llm_call_record(row: sqlite3.Row) -> LLMCallRecord:
        return LLMCallRecord(
            call_id=row["call_id"],
            run_id=row["run_id"],
            lease_generation=row["lease_generation"],
            role=row["role"],
            prompt_version=row["prompt_version"],
            request_hash=row["request_hash"],
            configured_model=row["configured_model"],
            status=row["status"],
            reserved_input_tokens=row["reserved_input_tokens"],
            reserved_output_tokens=row["reserved_output_tokens"],
            actual_model=row["actual_model"],
            response_id=row["response_id"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            error_type=row["error_type"],
            created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
            completed_at=_datetime(row["completed_at"]),
        )

    def request_cancel(self, run_id: str, *, reason: str | None = None) -> RunRecord:
        """Persist a cancellation request without performing worker cleanup."""

        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            run = self._get_run(connection, run_id)
            if run.status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED}:
                return run
            validate_transition(run.status, RunStatus.CANCEL_REQUESTED)
            connection.execute(
                """
                UPDATE runs SET status = ?, terminal_reason = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    RunStatus.CANCEL_REQUESTED.value,
                    reason or "Cancelled by user",
                    now,
                    run_id,
                    run.status.value,
                ),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                node="api",
                event_type="run.cancel_requested",
                summary="Cancellation requested",
                artifact_id=None,
                created_at=now,
            )
            return self._get_run(connection, run_id)

    def finalize_next_cancellation(self) -> RunRecord | None:
        """Move one requested cancellation to its durable terminal state.

        Cancellation is a control-plane operation, so it deliberately does not
        require the worker lease that the request has already invalidated.  The
        immediate transaction serializes this reaper with claims and terminal
        writes, and clearing the lease prevents a cancelled generation from
        being mistaken for active work.
        """

        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status = ?
                ORDER BY updated_at ASC, run_id ASC
                LIMIT 1
                """,
                (RunStatus.CANCEL_REQUESTED.value,),
            ).fetchone()
            if row is None:
                return None
            validate_transition(RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED)
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, terminal_reason = COALESCE(terminal_reason, ?),
                    ended_at = ?, updated_at = ?,
                    worker_id = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                WHERE run_id = ? AND status = ?
                """,
                (
                    RunStatus.CANCELLED.value,
                    "Cancelled by user",
                    now,
                    now,
                    row["run_id"],
                    RunStatus.CANCEL_REQUESTED.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_event(
                connection,
                run_id=row["run_id"],
                node="worker",
                event_type="run.cancelled",
                summary="Cancellation finalized",
                artifact_id=None,
                created_at=now,
            )
            return self._get_run(connection, row["run_id"])

    def transition_fenced(
        self,
        token: LeaseToken,
        target: RunStatus,
        *,
        reason: str | None = None,
        current_node: str | None = None,
    ) -> RunRecord:
        now = _timestamp(self._clock())
        with self._transaction(immediate=True) as connection:
            row = self._require_active_lease(connection, token, now)
            current = RunStatus(row["status"])
            validate_transition(current, target)
            terminal = target in TERMINAL_STATUSES
            release = target is not RunStatus.RUNNING
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, current_node = COALESCE(?, current_node),
                    terminal_reason = ?, ended_at = ?, updated_at = ?,
                    worker_id = CASE WHEN ? THEN NULL ELSE worker_id END,
                    lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                    heartbeat_at = CASE WHEN ? THEN NULL ELSE heartbeat_at END
                WHERE run_id = ? AND worker_id = ? AND lease_generation = ?
                  AND status = ? AND lease_expires_at > ?
                """,
                (
                    target.value,
                    current_node,
                    reason if terminal else None,
                    now if terminal else None,
                    now,
                    release,
                    release,
                    release,
                    token.run_id,
                    token.worker_id,
                    token.generation,
                    RunStatus.RUNNING.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(token.run_id, token.worker_id, token.generation)
            self._insert_event(
                connection,
                run_id=token.run_id,
                node="worker",
                event_type="run.status_changed",
                summary=f"Run transitioned from {current.value} to {target.value}",
                artifact_id=None,
                created_at=now,
            )
            return self._get_run(connection, token.run_id)
