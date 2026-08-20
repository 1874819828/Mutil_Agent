"""Immutable records returned by the SQLite run store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.contracts import ApprovalOutcome, RunStatus, TaskPlan


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    project_id: str
    project_profile_hash: str
    requirement: str
    status: RunStatus
    current_node: str | None
    plan: TaskPlan | None
    plan_hash: str | None
    worker_id: str | None
    lease_generation: int
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    terminal_reason: str | None
    last_event_id: int | None
    parent_run_id: str | None = None
    source_manifest_hash: str | None = None
    context_bundle_hash: str | None = None
    acceptance_pack_id: str | None = None
    acceptance_pack_hash: str | None = None
    provider_mode: str = "mock"


@dataclass(frozen=True, slots=True)
class LeaseToken:
    """A fencing token. Identity and generation, not expiry, authorize writes."""

    run_id: str
    worker_id: str
    generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    run: RunRecord
    token: LeaseToken


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: int
    run_id: str
    node: str
    event_type: str
    summary: str
    artifact_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    kind: str
    relative_path: str
    sha256: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    run_id: str
    decision: ApprovalOutcome
    plan_hash: str
    project_profile_hash: str
    idempotency_key: str
    reason: str | None
    created_at: datetime
    source_manifest_hash: str | None = None
    context_bundle_hash: str | None = None
    acceptance_pack_hash: str | None = None


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """Audit metadata for one provider attempt; never contains prompts or source."""

    call_id: str
    run_id: str
    lease_generation: int
    role: str
    prompt_version: str
    request_hash: str
    configured_model: str
    status: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    actual_model: str | None
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    error_type: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """Durable audit record for the separately approved source publication."""

    run_id: str
    status: str
    project_profile_hash: str
    source_manifest_hash: str
    diff_sha256: str
    request_hash: str
    idempotency_key: str
    changed_files: tuple[str, ...]
    backup_relative_path: str | None
    published_manifest_hash: str | None
    git_original_branch: str | None
    git_base_commit: str | None
    git_branch: str | None
    git_commit: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
