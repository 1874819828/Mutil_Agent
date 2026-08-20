"""Application integration layer for the first executable vertical slice.

The module wires persistent run leases, LangGraph checkpoints, the frozen
project profile, isolated workspaces, role nodes, and the test runner.  It is
deliberately synchronous at its public boundary so FastAPI can run it from a
single background thread without allowing concurrent access to a run.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Mapping
from uuid import uuid4

from app.acceptance import AcceptancePack, AcceptancePackRegistry
from app.config import FrozenProjectProfile, load_frozen_project_profile
from app.config.llm_settings import LLMSettings, ProviderMode
from app.context import (
    InspectionRequest as ContextInspectionRequest,
    RepositoryContextService,
)
from app.contracts import (
    ApprovalOutcome,
    ChangeSet,
    FileOperation,
    RunStatus,
    TaskPlan,
    canonical_json_hash,
)
from app.graph import WorkflowNodes, build_workflow
from app.git_delivery import (
    GitDeliveryError,
    GitDeliveryService,
    GitDeliverySession,
)
from app.llm import (
    AgentRole,
    DeterministicMockProvider,
    FencedLLMCallExecutor,
    LLMProvider,
)
from app.nodes import (
    DeveloperNode,
    ManagerContextNode,
    ManagerNode,
    PolicyGateNode,
    ReviewerNode,
    TesterNode,
)
from app.sandbox import RunnerRequest, TestRunner
from app.storage import (
    ApprovalRecord,
    ArtifactRecord,
    LeaseToken,
    PublicationRecord,
    RunRecord,
    SQLiteRunStore,
)
from app.workspace import (
    EffectiveWorkspacePolicy,
    WorkspaceService,
    WorkspaceSnapshot,
    build_effective_policy,
    build_manifest,
    generate_unified_diff,
    resolve_within,
    validate_write_path,
)


_SAFE_ARTIFACT_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_POSSIBLE_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*['\"][^'\"]{8,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
)


class RuntimeConfigurationError(RuntimeError):
    """The local runtime cannot safely start with the supplied configuration."""


class AtomicChangeError(RuntimeError):
    """A ChangeSet could not be applied atomically to the isolated workspace."""


class PublicationValidationError(RuntimeError):
    """A completed run cannot be safely published to its bound project."""


@dataclass(frozen=True, slots=True)
class PublicationPreview:
    run_id: str
    status: str
    eligible: bool
    reason: str | None
    project_id: str
    source_path: str
    project_profile_hash: str
    source_manifest_hash: str
    current_source_manifest_hash: str | None
    diff_sha256: str
    changed_files: tuple[str, ...]
    git_ready: bool
    git_current_branch: str | None
    git_base_commit: str | None
    git_target_branch: str | None
    git_commit: str | None


class _LeaseHeartbeat:
    """Renew a claimed generation while slow LLM or Docker nodes are running."""

    def __init__(
        self,
        *,
        store: SQLiteRunStore,
        token: LeaseToken,
        lease_ttl: timedelta,
        on_lost,
    ) -> None:
        self._store = store
        self._token = token
        self._lease_ttl = lease_ttl
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._lost: Exception | None = None
        self._thread = threading.Thread(
            target=self._loop,
            name=f"lease-heartbeat-{token.run_id}-g{token.generation}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._lease_ttl.total_seconds() / 3))

    def raise_if_lost(self) -> None:
        if self._lost is not None:
            raise self._lost

    def _loop(self) -> None:
        interval = max(0.1, self._lease_ttl.total_seconds() / 3)
        while not self._stop.wait(interval):
            try:
                self._token = self._store.renew_lease(
                    self._token, lease_ttl=self._lease_ttl
                )
            except Exception as exc:
                try:
                    current = self._store.get_run(self._token.run_id)
                except Exception:
                    current = None
                if (
                    current is not None
                    and current.lease_generation == self._token.generation
                    and current.status is not RunStatus.RUNNING
                ):
                    return
                self._lost = exc
                try:
                    self._on_lost()
                finally:
                    self._stop.set()


class _FencedArtifactSink:
    def __init__(
        self,
        *,
        runtime_root: Path,
        store: SQLiteRunStore,
        token: LeaseToken,
    ) -> None:
        self._runtime_root = runtime_root
        self._store = store
        self._token = token

    def write_text(self, *, run_id: str, name: str, content: str) -> str:
        if run_id != self._token.run_id:
            raise ValueError("artifact run_id does not match the active lease")
        if not _SAFE_ARTIFACT_NAME.fullmatch(name):
            raise ValueError("artifact name is not portable or safe")
        self._store.assert_active_lease(self._token)
        relative = Path("runs") / run_id / "artifacts" / (
            f"g{self._token.generation}-{name}"
        )
        target = self._runtime_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds the fixed 8 MiB size limit")
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            self._store.assert_active_lease(self._token)
            os.replace(temporary, target)
            record = self._store.register_artifact_fenced(
                self._token,
                kind=name,
                relative_path=relative.as_posix(),
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return record.artifact_id


class _WorkspaceChangeNode:
    def __init__(
        self,
        *,
        profile: FrozenProjectProfile,
        snapshot: WorkspaceSnapshot,
        store: SQLiteRunStore,
        token: LeaseToken,
    ) -> None:
        self._profile = profile
        self._snapshot = snapshot
        self._store = store
        self._token = token

    def __call__(self, state: Mapping[str, Any]) -> dict[str, Any]:
        self._store.assert_active_lease(self._token)
        plan = TaskPlan.model_validate(_model_dump(state["plan"]))
        change_set = ChangeSet.model_validate(_model_dump(state["change_set"]))
        policy = build_effective_policy(self._profile, plan.allowed_change_globs)
        self._apply_atomic(change_set, policy)
        self._store.assert_active_lease(self._token)
        self._snapshot_service().assert_source_unchanged(self._snapshot)
        diff = generate_unified_diff(
            self._snapshot.source_root,
            self._snapshot.workspace_root,
            policy,
            self._profile.limits,
        )
        repository_files = _read_selected_files(
            self._snapshot.workspace_root,
            {
                *plan.files_to_inspect,
                *(change.path for change in change_set.changes),
            },
            build_effective_policy(self._profile),
        )
        return {
            "diff": diff.text,
            "changed_files": list(diff.changed_files),
            "effective_change_globs": list(plan.allowed_change_globs),
            "repository_files": repository_files,
            "scope_violations": [],
            "baseline_modified": False,
            "secret_findings": _secret_findings(diff.text),
        }

    def _apply_atomic(
        self, change_set: ChangeSet, policy: EffectiveWorkspacePolicy
    ) -> None:
        originals: dict[Path, bytes | None] = {}
        created_parents: set[Path] = set()
        try:
            for change in change_set.changes:
                self._store.assert_active_lease(self._token)
                target = validate_write_path(
                    self._snapshot.workspace_root,
                    change.path,
                    policy,
                    must_exist=change.operation is FileOperation.UPDATE,
                )
                if target not in originals:
                    originals[target] = target.read_bytes() if target.exists() else None
                if change.operation is FileOperation.CREATE and target.exists():
                    raise AtomicChangeError(f"create target already exists: {change.path}")
                if change.operation is FileOperation.UPDATE:
                    current = target.read_bytes()
                    current_hash = hashlib.sha256(current).hexdigest()
                    if current_hash != change.base_sha256:
                        raise AtomicChangeError(
                            f"base_sha256 mismatch for {change.path}; refusing stale change"
                        )
                data = change.content.encode("utf-8")
                if b"\x00" in data or len(data) > self._profile.limits.max_file_bytes:
                    raise AtomicChangeError(f"unsafe or oversized content: {change.path}")
                if not target.parent.exists():
                    target.parent.mkdir(parents=True)
                    created_parents.add(target.parent)
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix=f".{target.name}.", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._store.assert_active_lease(self._token)
                os.replace(temporary, target)
            build_manifest(
                self._snapshot.workspace_root,
                build_effective_policy(self._profile),
                self._profile.limits,
                reject_unallowed=True,
            )
        except Exception as exc:
            for target, original in reversed(tuple(originals.items())):
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original)
            for parent in sorted(created_parents, key=lambda item: len(item.parts), reverse=True):
                try:
                    parent.rmdir()
                except OSError:
                    pass
            if isinstance(exc, AtomicChangeError):
                raise
            raise AtomicChangeError(str(exc)) from exc

    @staticmethod
    def _snapshot_service() -> WorkspaceService:
        return WorkspaceService()


class _WorkspacePolicyChecks:
    def __init__(
        self,
        *,
        profile: FrozenProjectProfile,
        snapshot: WorkspaceSnapshot,
        workspace_service: WorkspaceService,
    ) -> None:
        self._profile = profile
        self._snapshot = snapshot
        self._workspace_service = workspace_service

    def evaluate(self, state: Mapping[str, Any]) -> list[str]:
        violations: list[str] = []
        try:
            self._workspace_service.assert_source_unchanged(self._snapshot)
            plan = TaskPlan.model_validate(_model_dump(state["plan"]))
            policy = build_effective_policy(self._profile, plan.allowed_change_globs)
            current = generate_unified_diff(
                self._snapshot.source_root,
                self._snapshot.workspace_root,
                policy,
                self._profile.limits,
            )
            if current.text != str(state.get("diff", "")):
                violations.append("workspace changed after the recorded diff")
        except Exception as exc:
            violations.append(f"workspace verification failed: {exc}")
        return violations


class AssistantRuntime:
    """Facade used by the API and deterministic integration tests."""

    def __init__(
        self,
        *,
        runtime_root: str | Path,
        profile_paths: Mapping[str, str | Path],
        runner: TestRunner,
        worker_id: str = "local-worker",
        provider: LLMProvider | None = None,
        llm_settings: LLMSettings | None = None,
        acceptance_registry: AcceptancePackRegistry | None = None,
        git_delivery: GitDeliveryService | None = None,
        lease_seconds: int = 300,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve(strict=False)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._profile_paths = {
            project_id: Path(path).resolve(strict=True)
            for project_id, path in profile_paths.items()
        }
        if not self._profile_paths:
            raise RuntimeConfigurationError("at least one project profile is required")
        self._runner = runner
        self._worker_id = worker_id
        self._provider = provider or DeterministicMockProvider()
        self._llm_settings = llm_settings
        self._provider_mode = (
            llm_settings.provider_mode.value if llm_settings is not None else "mock"
        )
        self._acceptance_registry = acceptance_registry
        self._lease_ttl = timedelta(seconds=lease_seconds)
        self.store = SQLiteRunStore(self.runtime_root / "assistant.sqlite3")
        self._workspace_service = WorkspaceService()
        self._git_delivery = git_delivery or GitDeliveryService()
        self._publication_lock = threading.Lock()
        self._recover_incomplete_publications()

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profile_paths))

    def runner_status(self) -> dict[str, str]:
        try:
            identity = self._runner.preflight()
        except Exception as exc:
            return {"status": "unavailable", "reason": _safe_error(exc)}
        return {"status": "ready", "identity": str(identity or "configured")}

    def create_run(
        self,
        *,
        project_id: str,
        requirement: str,
        acceptance_pack_id: str | None = None,
    ) -> RunRecord:
        profile = self._load_profile(project_id)
        pack = self._resolve_acceptance_pack(
            acceptance_pack_id, project_id=project_id, requirement=requirement
        )
        if self._provider_mode != ProviderMode.MOCK.value and pack is None:
            raise RuntimeConfigurationError(
                "real LLM runs require a ready acceptance_pack_id"
            )
        run_id = str(uuid4())
        snapshot = self._workspace_service.create_workspace(
            profile, self._source_snapshot_path(run_id)
        )
        return self.store.create_run(
            run_id=run_id,
            project_id=project_id,
            requirement=requirement,
            project_profile_hash=profile.profile_hash,
            source_manifest_hash=snapshot.source_manifest.digest,
            acceptance_pack_id=pack.pack_id if pack else None,
            acceptance_pack_hash=pack.suite_hash if pack else None,
            provider_mode=self._provider_mode,
        )

    def get_run(self, run_id: str) -> RunRecord:
        return self.store.get_run(run_id)

    def list_acceptance_packs(self) -> tuple[AcceptancePack, ...]:
        if self._acceptance_registry is None:
            return ()
        return self._acceptance_registry.list()

    def approve_run(
        self,
        run_id: str,
        *,
        decision: str,
        plan_hash: str,
        project_profile_hash: str,
        source_manifest_hash: str | None = None,
        context_bundle_hash: str | None = None,
        acceptance_pack_hash: str | None = None,
        idempotency_key: str,
        reason: str | None = None,
    ) -> RunRecord:
        current = self.store.get_run(run_id)
        for label, expected, supplied in (
            ("source_manifest_hash", current.source_manifest_hash, source_manifest_hash),
            ("context_bundle_hash", current.context_bundle_hash, context_bundle_hash),
            ("acceptance_pack_hash", current.acceptance_pack_hash, acceptance_pack_hash),
        ):
            if expected != supplied:
                raise ValueError(f"approval {label} does not match the frozen run")
        return self.store.decide_plan(
            run_id,
            decision=ApprovalOutcome(decision),
            plan_hash=plan_hash,
            project_profile_hash=project_profile_hash,
            source_manifest_hash=source_manifest_hash,
            context_bundle_hash=context_bundle_hash,
            acceptance_pack_hash=acceptance_pack_hash,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    def request_cancel(self, run_id: str, *, reason: str | None = None) -> RunRecord:
        current = self.store.get_run(run_id)
        requested = self.store.request_cancel(run_id, reason=reason)
        if current.status is RunStatus.RUNNING and current.lease_generation > 0:
            self._cancel_generation(run_id, current.lease_generation)
        return requested

    def process_next(self, *, raise_errors: bool = True) -> RunRecord | None:
        cancelled = self.store.finalize_next_cancellation()
        if cancelled is not None:
            self._remove_generation_containers(cancelled.run_id)
            return cancelled
        claimed = self.store.claim_next_run(
            self._worker_id, lease_ttl=self._lease_ttl
        )
        if claimed is None:
            return None
        heartbeat = _LeaseHeartbeat(
            store=self.store,
            token=claimed.token,
            lease_ttl=self._lease_ttl,
            on_lost=lambda: self._cancel_generation(
                claimed.token.run_id, claimed.token.generation
            ),
        )
        heartbeat.start()
        try:
            if claimed.run.plan is None:
                result = self._produce_plan(claimed.run, claimed.token)
            else:
                result = self._resume_approved_run(claimed.run, claimed.token)
            heartbeat.raise_if_lost()
            return result
        except Exception as exc:
            try:
                failed = self.store.transition_fenced(
                    claimed.token,
                    RunStatus.FAILED,
                    reason=_safe_error(exc),
                    current_node="runtime_error",
                )
            except Exception:
                if raise_errors:
                    raise
                return self.store.get_run(claimed.run.run_id)
            if raise_errors:
                raise
            return failed
        finally:
            heartbeat.stop()

    def _remove_generation_containers(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run.lease_generation > 0:
            self._cancel_generation(run_id, run.lease_generation)

    def _cancel_generation(self, run_id: str, generation: int) -> None:
        cancel = getattr(self._runner, "cancel", None)
        if callable(cancel):
            try:
                cancel(run_id, generation)
            except Exception:
                # Fencing already prevents publication. Container cleanup is
                # best effort and targets a validated generation-specific name.
                pass

    def source_manifest_digest(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        if run.source_manifest_hash is None:
            raise RuntimeError("run source snapshot is not bound")
        return run.source_manifest_hash

    def read_source_file(self, run_id: str, relative_path: str) -> str:
        run = self.store.get_run(run_id)
        profile = self._load_profile(run.project_id)
        policy = build_effective_policy(profile)
        normalized = policy.require_allowed(relative_path)
        return (self._source_snapshot_path(run_id) / Path(normalized)).read_text(
            encoding="utf-8"
        )

    def get_diff(self, run_id: str) -> str:
        return self._read_named_artifact(run_id, "change.patch")

    def get_report(self, run_id: str) -> dict[str, Any]:
        return json.loads(self._read_named_artifact(run_id, "report.json"))

    def publication_preview(self, run_id: str) -> PublicationPreview:
        """Return a read-only, hash-bound publication decision for one run."""

        run = self.store.get_run(run_id)
        profile = self._verified_profile(run)
        existing = self.store.get_publication(run_id)
        status = existing.status if existing is not None else "not_published"
        source_hash = run.source_manifest_hash or ""
        diff_sha256 = ""
        changed_files: tuple[str, ...] = ()
        current_hash: str | None = None
        reason: str | None = None
        git_ready = False
        git_current_branch = (
            existing.git_original_branch if existing is not None else None
        )
        git_base_commit = existing.git_base_commit if existing is not None else None
        git_target_branch = existing.git_branch if existing is not None else None
        git_commit = existing.git_commit if existing is not None else None

        if run.status is not RunStatus.COMPLETED:
            reason = "run is not completed"
        elif run.plan is None or not source_hash:
            reason = "completed run is missing its approved plan or source binding"
        else:
            try:
                policy = build_effective_policy(profile, run.plan.allowed_change_globs)
                workspace = self._final_workspace(run)
                diff = generate_unified_diff(
                    self._source_snapshot_path(run_id),
                    workspace,
                    policy,
                    profile.limits,
                )
                diff_sha256 = diff.sha256
                changed_files = diff.changed_files
                current = build_manifest(
                    profile.source_path,
                    build_effective_policy(profile),
                    profile.limits,
                )
                current_hash = current.digest
                if existing is not None and existing.status in {
                    "publishing",
                    "published",
                    "manual_recovery_required",
                }:
                    reason = f"run publication is already {existing.status}"
                elif not changed_files:
                    reason = "completed run contains no publishable file changes"
                elif diff.deleted_files:
                    reason = "file deletion publication is not supported"
                elif current_hash != source_hash:
                    reason = "project source changed after this run was created"
                else:
                    git_state = self._git_delivery.inspect(
                        profile.source_path, run_id=run_id
                    )
                    git_ready = git_state.ready
                    git_current_branch = git_state.current_branch
                    git_base_commit = git_state.head_commit
                    git_target_branch = git_state.target_branch
                    if not git_state.ready:
                        reason = git_state.reason or "Git repository is not ready"
            except Exception:
                reason = "project source or completed workspace cannot be safely verified"

        return PublicationPreview(
            run_id=run_id,
            status=status,
            eligible=reason is None,
            reason=reason,
            project_id=run.project_id,
            source_path=str(profile.source_path),
            project_profile_hash=run.project_profile_hash,
            source_manifest_hash=source_hash,
            current_source_manifest_hash=current_hash,
            diff_sha256=diff_sha256,
            changed_files=changed_files,
            git_ready=git_ready,
            git_current_branch=git_current_branch,
            git_base_commit=git_base_commit,
            git_target_branch=git_target_branch,
            git_commit=git_commit,
        )

    def publish_run(
        self,
        run_id: str,
        *,
        confirmation: str,
        project_profile_hash: str,
        source_manifest_hash: str,
        diff_sha256: str,
        git_original_branch: str,
        git_base_commit: str,
        git_target_branch: str,
        idempotency_key: str,
        comment: str | None = None,
    ) -> PublicationRecord:
        """Publish a completed generation after an explicit second confirmation.

        Files are replaced one at a time on their own filesystem.  A durable
        rollback journal makes the multi-file operation recoverable; it is not
        represented as an impossible cross-file atomic rename.
        """

        if confirmation != "publish":
            raise PublicationValidationError("confirmation must be exactly 'publish'")
        request_hash = canonical_json_hash(
            {
                "run_id": run_id,
                "confirmation": confirmation,
                "project_profile_hash": project_profile_hash,
                "source_manifest_hash": source_manifest_hash,
                "diff_sha256": diff_sha256,
                "git_original_branch": git_original_branch,
                "git_base_commit": git_base_commit,
                "git_target_branch": git_target_branch,
                "comment": comment.strip() if comment else None,
            }
        )
        existing = self.store.get_publication(run_id)
        if (
            existing is not None
            and existing.status == "published"
            and existing.idempotency_key == idempotency_key
            and existing.request_hash == request_hash
        ):
            return existing

        with self._publication_lock:
            preview = self.publication_preview(run_id)
            for label, expected, supplied in (
                ("project profile", preview.project_profile_hash, project_profile_hash),
                ("source manifest", preview.source_manifest_hash, source_manifest_hash),
                ("diff", preview.diff_sha256, diff_sha256),
            ):
                if not expected or supplied != expected:
                    raise PublicationValidationError(
                        f"{label} hash does not match the reviewed publication preview"
                    )
            if not preview.eligible:
                raise PublicationValidationError(preview.reason or "publication is not eligible")
            for label, expected, supplied in (
                ("Git original branch", preview.git_current_branch, git_original_branch),
                ("Git base commit", preview.git_base_commit, git_base_commit),
                ("Git target branch", preview.git_target_branch, git_target_branch),
            ):
                if not expected or supplied != expected:
                    raise PublicationValidationError(
                        f"{label} hash does not match the reviewed publication preview"
                    )
            run = self.store.get_run(run_id)
            profile = self._verified_profile(run)
            self._assert_acceptance_pack_unchanged(run)
            self._runner.preflight()
            workspace = self._final_workspace(run)
            policy = build_effective_policy(profile, run.plan.allowed_change_globs)  # type: ignore[union-attr]
            diff = generate_unified_diff(
                self._source_snapshot_path(run_id), workspace, policy, profile.limits
            )
            attempt_name = f"attempt-{uuid4()}"
            attempt_relative = Path("runs") / run_id / "publication" / attempt_name
            attempt_root = self.runtime_root / attempt_relative
            attempt_root.mkdir(parents=True, exist_ok=False)
            journal, originals, desired = self._prepare_publication_journal(
                profile=profile,
                run=run,
                workspace=workspace,
                changed_files=diff.changed_files,
                attempt_root=attempt_root,
            )
            journal_relative = (attempt_relative / "journal.json").as_posix()
            _atomic_write_bytes(
                attempt_root / "journal.json",
                json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True).encode(
                    "utf-8"
                ),
            )
            record = self.store.begin_publication(
                run_id=run_id,
                project_profile_hash=project_profile_hash,
                source_manifest_hash=source_manifest_hash,
                diff_sha256=diff_sha256,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
                changed_files=diff.changed_files,
                backup_relative_path=journal_relative,
                git_original_branch=git_original_branch,
                git_base_commit=git_base_commit,
                git_branch=git_target_branch,
            )
            if record.status != "publishing":
                return record

            wrote: list[str] = []
            git_session: GitDeliverySession | None = None
            try:
                git_session = self._git_delivery.start(
                    profile.source_path,
                    run_id=run_id,
                    expected_original_branch=git_original_branch,
                    expected_base_commit=git_base_commit,
                    expected_target_branch=git_target_branch,
                )
                for relative_path in diff.changed_files:
                    target = validate_write_path(
                        profile.source_path,
                        relative_path,
                        policy,
                        must_exist=originals[relative_path] is not None,
                    )
                    current = target.read_bytes() if target.exists() else None
                    if current != originals[relative_path]:
                        raise PublicationValidationError(
                            "project source changed while publication was starting"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    validate_write_path(
                        profile.source_path,
                        relative_path,
                        policy,
                        must_exist=False,
                    )
                    _atomic_write_bytes(target, desired[relative_path])
                    wrote.append(relative_path)

                expected = build_manifest(
                    workspace,
                    build_effective_policy(profile),
                    profile.limits,
                    reject_unallowed=True,
                )
                published = build_manifest(
                    profile.source_path,
                    build_effective_policy(profile),
                    profile.limits,
                )
                if published.digest != expected.digest:
                    raise PublicationValidationError(
                        "published project does not match the reviewed workspace"
                    )

                requests = self._publication_runner_requests(run, profile)
                results = {}
                for command_id in ("baseline_test", "acceptance_test"):
                    result = self._runner.run(requests[command_id])
                    results[command_id] = asdict(result)
                    if result.exit_code != 0 or result.failed:
                        raise PublicationValidationError(
                            f"post-publication verification failed: {command_id}"
                        )
                after_tests = build_manifest(
                    profile.source_path,
                    build_effective_policy(profile),
                    profile.limits,
                )
                if after_tests.digest != expected.digest:
                    raise PublicationValidationError(
                        "project source changed during post-publication verification"
                    )

                git_commit = self._git_delivery.commit(
                    git_session,
                    changed_files=diff.changed_files,
                    message=f"agent: publish run {run_id}",
                )

                report = {
                    "run_id": run_id,
                    "status": "published",
                    "project_id": run.project_id,
                    "project_profile_hash": project_profile_hash,
                    "source_manifest_hash": source_manifest_hash,
                    "diff_sha256": diff_sha256,
                    "published_manifest_hash": expected.digest,
                    "changed_files": list(diff.changed_files),
                    "git": {
                        "original_branch": git_original_branch,
                        "base_commit": git_base_commit,
                        "branch": git_target_branch,
                        "commit": git_commit,
                    },
                    "verification": results,
                    "comment": comment,
                }
                report_path = attempt_root / "publication-report.json"
                report_data = json.dumps(
                    report, ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8")
                _atomic_write_bytes(report_path, report_data)
                artifact = self.store.register_artifact(
                    run_id=run_id,
                    kind="publication-report.json",
                    relative_path=(attempt_relative / "publication-report.json").as_posix(),
                    sha256=hashlib.sha256(report_data).hexdigest(),
                    size_bytes=len(report_data),
                )
                completed = self.store.complete_publication(
                    run_id,
                    request_hash=request_hash,
                    published_manifest_hash=expected.digest,
                    git_commit=git_commit,
                )
                try:
                    self.store.append_event(
                        run_id,
                        node="publisher",
                        event_type="publication.published",
                        summary="Reviewed changes committed on an isolated Git branch and re-verified",
                        artifact_id=artifact.artifact_id,
                    )
                except Exception:
                    # Publication is already durably complete.  A secondary
                    # timeline write must never undo the verified Git commit.
                    pass
                return completed
            except Exception as exc:
                rollback_ok = self._rollback_publication(
                    profile=profile,
                    originals=originals,
                    desired=desired,
                    paths=tuple(reversed(wrote)),
                )
                if rollback_ok and git_session is not None:
                    try:
                        self._git_delivery.abort(git_session)
                    except GitDeliveryError:
                        rollback_ok = False
                try:
                    restored = build_manifest(
                        profile.source_path,
                        build_effective_policy(profile),
                        profile.limits,
                    )
                    rollback_ok = rollback_ok and restored.digest == source_manifest_hash
                except Exception:
                    rollback_ok = False
                message = _safe_error(exc)
                self.store.fail_publication(
                    run_id,
                    request_hash=request_hash,
                    error=message,
                    manual_recovery_required=not rollback_ok,
                )
                try:
                    self.store.append_event(
                        run_id,
                        node="publisher",
                        event_type="publication.failed",
                        summary=(
                            "Publication failed and source was rolled back"
                            if rollback_ok
                            else "Publication failed; manual source recovery is required"
                        ),
                    )
                except Exception:
                    pass
                raise PublicationValidationError(message) from exc

    def artifact_path(self, artifact_id: str) -> tuple[ArtifactRecord, Path]:
        record = self.store.get_artifact(artifact_id)
        root = self.runtime_root.resolve(strict=True)
        path = (root / Path(record.relative_path)).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("artifact path escaped runtime root") from exc
        return record, path

    def read_artifact(self, artifact_id: str) -> tuple[ArtifactRecord, bytes]:
        """Read and verify an artifact before exposing it as audit evidence."""

        record, path = self.artifact_path(artifact_id)
        if path.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(path)
        ) or not path.is_file():
            raise RuntimeError("artifact is not a regular file")
        data = path.read_bytes()
        if len(data) != record.size_bytes:
            raise RuntimeError("artifact size no longer matches its registry record")
        if hashlib.sha256(data).hexdigest() != record.sha256:
            raise RuntimeError("artifact hash no longer matches its registry record")
        return record, data

    def _read_named_artifact(self, run_id: str, kind: str) -> str:
        records = [
            record
            for record in self.store.list_artifacts(run_id)
            if record.kind == kind
        ]
        if not records:
            raise FileNotFoundError(kind)
        _, data = self.read_artifact(records[-1].artifact_id)
        return data.decode("utf-8")

    def _produce_plan(self, run: RunRecord, token: LeaseToken) -> RunRecord:
        return asyncio.run(self._produce_plan_async(run, token))

    async def _produce_plan_async(
        self, run: RunRecord, token: LeaseToken
    ) -> RunRecord:
        profile = self._verified_profile(run)
        snapshot = self._workspace(profile, run.run_id, token.generation)
        executor = self._llm_executor(token)
        context_service = RepositoryContextService(
            self._context_profile(profile, snapshot.source_root)
        )
        index = context_service.build_index()
        selector = ManagerContextNode(self._provider, executor=executor)
        requested = await selector(
            {
                "requirement": run.requirement,
                "repository_index": {
                    "file_count": len(index.entries),
                    "total_bytes": sum(entry.size for entry in index.entries),
                    "files": [entry.path for entry in index.entries],
                    "rejected_paths": list(index.rejected_paths),
                },
                "allowed_change_globs": list(profile.include_globs),
            }
        )
        bundle = context_service.build_bundle(
            index,
            ContextInspectionRequest(paths=tuple(item.path for item in requested.files)),
            round_number=1,
        )
        manager_state = {
            "requirement": run.requirement,
            "repository_summary": {
                "source_manifest_hash": index.digest,
                "context_bundle_hash": bundle.bundle_hash,
                "files": [
                    {
                        "path": item.path,
                        "sha256": item.sha256,
                        "size": item.size,
                        "content": item.content,
                    }
                    for item in bundle.files
                ],
            },
            "effective_change_globs": list(profile.include_globs),
            "test_profile": profile.runner,
        }
        result = await ManagerNode(self._provider, executor=executor)(manager_state)
        plan = TaskPlan.model_validate(_model_dump(result["plan"]))
        # Constructing the intersection is a deterministic authorization check;
        # a Manager-supplied **/* never expands the frozen profile.
        build_effective_policy(profile, plan.allowed_change_globs)
        inspected = {item.path.casefold() for item in bundle.files}
        if not {item.casefold() for item in plan.files_to_inspect}.issubset(inspected):
            raise RuntimeError("Manager plan references files outside approved context")
        return self.store.save_plan_and_wait_for_approval(
            token,
            plan,
            source_manifest_hash=run.source_manifest_hash,
            context_bundle_hash=bundle.bundle_hash,
            acceptance_pack_hash=run.acceptance_pack_hash,
        )

    def _resume_approved_run(self, run: RunRecord, token: LeaseToken) -> RunRecord:
        return asyncio.run(self._resume_approved_run_async(run, token))

    async def _resume_approved_run_async(
        self, run: RunRecord, token: LeaseToken
    ) -> RunRecord:
        profile = self._verified_profile(run)
        approval = self.store.get_approval(run.run_id)
        if approval.decision is not ApprovalOutcome.APPROVE:
            raise RuntimeError("only an approved plan may be resumed")
        self._runner.preflight()
        if run.plan is None or run.plan_hash is None:
            raise RuntimeError("approved run has no durable plan")
        if canonical_json_hash(run.plan.model_dump(mode="json")) != run.plan_hash:
            raise RuntimeError("persisted plan no longer matches plan_hash")
        if approval.plan_hash != run.plan_hash:
            raise RuntimeError("approval no longer matches the persisted plan")
        self._validate_approval_bindings(run, approval)
        self._assert_acceptance_pack_unchanged(run)
        snapshot = self._workspace(profile, run.run_id, token.generation)
        # Source-bearing ChangeSets and diffs remain generation-local and are
        # never serialized into a generic LangGraph checkpoint.
        graph = self._graph(profile, snapshot, token, checkpointer=None)
        result = await graph.ainvoke(
            self._resume_state(run, token, profile, snapshot, approval),
            config=self._graph_config(run.run_id, token.generation),
        )
        self._workspace_service.assert_source_unchanged(snapshot)
        status = RunStatus(result["status"])
        self._persist_result(token, result)
        return self.store.transition_fenced(
            token,
            status,
            reason=(result.get("final_result") or {}).get("reason"),
            current_node="finalize",
        )

    def _graph(
        self,
        profile: FrozenProjectProfile,
        snapshot: WorkspaceSnapshot,
        token: LeaseToken,
        checkpointer,
    ):
        sink = _FencedArtifactSink(
            runtime_root=self.runtime_root,
            store=self.store,
            token=token,
        )
        executor = self._llm_executor(token)
        nodes = WorkflowNodes(
            manager=ManagerNode(self._provider, executor=executor),
            developer=DeveloperNode(self._provider, executor=executor),
            apply_change=_WorkspaceChangeNode(
                profile=profile,
                snapshot=snapshot,
                store=self.store,
                token=token,
            ),
            baseline_tester=TesterNode(
                self._runner,
                "baseline_test",
                result_key="baseline_test_result",
                artifact_sink=sink,
            ),
            acceptance_tester=TesterNode(
                self._runner,
                "acceptance_test",
                result_key="acceptance_test_result",
                artifact_sink=sink,
            ),
            policy_gate=PolicyGateNode(
                _WorkspacePolicyChecks(
                    profile=profile,
                    snapshot=snapshot,
                    workspace_service=self._workspace_service,
                )
            ),
            reviewer=ReviewerNode(self._provider, executor=executor),
        )
        return build_workflow(nodes, checkpointer=checkpointer)

    def _workspace(
        self, profile: FrozenProjectProfile, run_id: str, generation: int
    ) -> WorkspaceSnapshot:
        destination = (
            self.runtime_root
            / "runs"
            / run_id
            / "attempts"
            / f"g{generation}"
            / "workspace"
        )
        try:
            run = self.store.get_run(run_id)
        except Exception:
            # Backward-compatible internal helper for isolated workspace tests.
            return self._workspace_service.create_workspace(profile, destination)
        if run.source_manifest_hash is None:
            raise RuntimeError("run source snapshot is not bound")
        return self._workspace_service.create_workspace_from_snapshot(
            profile,
            self._source_snapshot_path(run_id),
            destination,
            expected_manifest_hash=run.source_manifest_hash,
        )

    def _initial_state(
        self,
        run: RunRecord,
        token: LeaseToken,
        profile: FrozenProjectProfile,
        snapshot: WorkspaceSnapshot,
    ) -> dict[str, Any]:
        repository_files = _read_manifest_files(
            snapshot.workspace_root, snapshot.workspace_manifest
        )
        return {
            "run_id": run.run_id,
            "project_id": run.project_id,
            "project_profile_hash": profile.profile_hash,
            "source_manifest_hash": run.source_manifest_hash,
            "context_bundle_hash": run.context_bundle_hash,
            "acceptance_pack_hash": run.acceptance_pack_hash,
            "effective_change_globs": list(profile.include_globs),
            "requirement": run.requirement,
            "status": RunStatus.RUNNING,
            "workspace_path": str(snapshot.workspace_root),
            "repository_summary": {
                "file_count": len(snapshot.workspace_manifest.entries),
                "total_bytes": snapshot.workspace_manifest.total_bytes,
                "files": [entry.path for entry in snapshot.workspace_manifest.entries],
            },
            "repository_files": repository_files,
            "test_profile": profile.runner,
            "runner_requests": self._runner_requests(run, token, snapshot),
            "developer_run_count": 0,
            "test_run_count": 0,
            "test_fix_count": 0,
            "review_fix_count": 0,
            "metrics": {},
        }

    def _resume_state(
        self,
        run: RunRecord,
        token: LeaseToken,
        profile: FrozenProjectProfile,
        snapshot: WorkspaceSnapshot,
        approval: ApprovalRecord,
    ) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "project_id": run.project_id,
            "requirement": run.requirement,
            "status": RunStatus.RUNNING,
            "workspace_path": str(snapshot.workspace_root),
            "repository_summary": {
                "file_count": len(snapshot.workspace_manifest.entries),
                "total_bytes": snapshot.workspace_manifest.total_bytes,
                "files": [entry.path for entry in snapshot.workspace_manifest.entries],
            },
            "repository_files": _read_selected_files(
                snapshot.workspace_root,
                run.plan.files_to_inspect if run.plan is not None else (),
                build_effective_policy(profile),
            ),
            "runner_requests": self._runner_requests(run, token, snapshot),
            "project_profile_hash": profile.profile_hash,
            "source_manifest_hash": run.source_manifest_hash,
            "context_bundle_hash": run.context_bundle_hash,
            "acceptance_pack_hash": run.acceptance_pack_hash,
            "effective_change_globs": list(profile.include_globs),
            "test_profile": profile.runner,
            "plan": run.plan,
            "plan_hash": run.plan_hash,
            "approval": {
                "decision": approval.decision.value,
                "plan_hash": approval.plan_hash,
                "project_profile_hash": approval.project_profile_hash,
                "source_manifest_hash": approval.source_manifest_hash,
                "context_bundle_hash": approval.context_bundle_hash,
                "acceptance_pack_hash": approval.acceptance_pack_hash,
            },
            "approval_prevalidated": True,
            "developer_run_count": 0,
            "test_run_count": 0,
            "test_fix_count": 0,
            "review_fix_count": 0,
            "metrics": {},
        }

    def _final_workspace(self, run: RunRecord) -> Path:
        if run.lease_generation <= 0:
            raise PublicationValidationError("completed run has no final generation")
        workspace = (
            self.runtime_root
            / "runs"
            / run.run_id
            / "attempts"
            / f"g{run.lease_generation}"
            / "workspace"
        )
        if not workspace.is_dir():
            raise PublicationValidationError("completed run workspace is unavailable")
        return workspace.resolve(strict=True)

    def _prepare_publication_journal(
        self,
        *,
        profile: FrozenProjectProfile,
        run: RunRecord,
        workspace: Path,
        changed_files: tuple[str, ...],
        attempt_root: Path,
    ) -> tuple[dict[str, Any], dict[str, bytes | None], dict[str, bytes]]:
        policy = build_effective_policy(profile, run.plan.allowed_change_globs)  # type: ignore[union-attr]
        originals: dict[str, bytes | None] = {}
        desired: dict[str, bytes] = {}
        entries: list[dict[str, Any]] = []
        for relative_path in changed_files:
            source_file = validate_write_path(
                self._source_snapshot_path(run.run_id),
                relative_path,
                policy,
                must_exist=False,
            )
            workspace_file = validate_write_path(
                workspace, relative_path, policy, must_exist=True
            )
            original = source_file.read_bytes() if source_file.exists() else None
            replacement = workspace_file.read_bytes()
            if len(replacement) > profile.limits.max_file_bytes or b"\x00" in replacement:
                raise PublicationValidationError(
                    f"publishable file is unsafe or oversized: {relative_path}"
                )
            originals[relative_path] = original
            desired[relative_path] = replacement
            original_backup: str | None = None
            if original is not None:
                original_path = attempt_root / "original" / Path(relative_path)
                _atomic_write_bytes(original_path, original)
                original_backup = (Path("original") / Path(relative_path)).as_posix()
            desired_path = attempt_root / "desired" / Path(relative_path)
            _atomic_write_bytes(desired_path, replacement)
            entries.append(
                {
                    "path": relative_path,
                    "original_backup": original_backup,
                    "original_sha256": (
                        hashlib.sha256(original).hexdigest()
                        if original is not None
                        else None
                    ),
                    "desired_backup": (
                        Path("desired") / Path(relative_path)
                    ).as_posix(),
                    "desired_sha256": hashlib.sha256(replacement).hexdigest(),
                }
            )
        return (
            {
                "version": 1,
                "run_id": run.run_id,
                "source_manifest_hash": run.source_manifest_hash,
                "entries": entries,
            },
            originals,
            desired,
        )

    def _rollback_publication(
        self,
        *,
        profile: FrozenProjectProfile,
        originals: Mapping[str, bytes | None],
        desired: Mapping[str, bytes],
        paths: tuple[str, ...],
    ) -> bool:
        policy = build_effective_policy(profile)
        ok = True
        for relative_path in paths:
            try:
                target = validate_write_path(
                    profile.source_path,
                    relative_path,
                    policy,
                    must_exist=False,
                )
                current = target.read_bytes() if target.exists() else None
                original = originals[relative_path]
                if current == original:
                    continue
                if current != desired[relative_path]:
                    ok = False
                    continue
                if original is None:
                    target.unlink()
                else:
                    _atomic_write_bytes(target, original)
            except Exception:
                ok = False
        return ok

    def _recover_incomplete_publications(self) -> None:
        """Rollback interrupted publications without overwriting third-party edits."""

        for record in self.store.list_incomplete_publications():
            manual = True
            error = "interrupted publication requires manual recovery"
            try:
                if record.backup_relative_path is None:
                    raise ValueError("publication journal path is missing")
                run = self.store.get_run(record.run_id)
                profile = self._verified_profile(run)
                journal_path = resolve_within(
                    self.runtime_root, record.backup_relative_path, must_exist=True
                )
                raw = json.loads(journal_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(raw, dict)
                    or raw.get("version") != 1
                    or raw.get("run_id") != record.run_id
                    or raw.get("source_manifest_hash") != record.source_manifest_hash
                    or not isinstance(raw.get("entries"), list)
                ):
                    raise ValueError("publication journal binding is invalid")
                originals: dict[str, bytes | None] = {}
                desired: dict[str, bytes] = {}
                paths: list[str] = []
                for entry in raw["entries"]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                        raise ValueError("publication journal entry is invalid")
                    relative_path = entry["path"]
                    build_effective_policy(profile).require_allowed(relative_path)
                    desired_path = resolve_within(
                        journal_path.parent, entry["desired_backup"], must_exist=True
                    )
                    replacement = desired_path.read_bytes()
                    if hashlib.sha256(replacement).hexdigest() != entry["desired_sha256"]:
                        raise ValueError("publication desired backup hash mismatch")
                    original_backup = entry.get("original_backup")
                    if original_backup is None:
                        original = None
                    else:
                        original_path = resolve_within(
                            journal_path.parent, original_backup, must_exist=True
                        )
                        original = original_path.read_bytes()
                        if hashlib.sha256(original).hexdigest() != entry["original_sha256"]:
                            raise ValueError("publication original backup hash mismatch")
                    paths.append(relative_path)
                    originals[relative_path] = original
                    desired[relative_path] = replacement
                rolled_back = self._rollback_publication(
                    profile=profile,
                    originals=originals,
                    desired=desired,
                    paths=tuple(reversed(paths)),
                )
                if rolled_back and all(
                    (
                        record.git_original_branch,
                        record.git_base_commit,
                        record.git_branch,
                    )
                ):
                    self._git_delivery.abort(
                        GitDeliverySession(
                            repository_root=profile.source_path,
                            original_branch=record.git_original_branch or "",
                            base_commit=record.git_base_commit or "",
                            target_branch=record.git_branch or "",
                        )
                    )
                restored = build_manifest(
                    profile.source_path,
                    build_effective_policy(profile),
                    profile.limits,
                )
                manual = not (
                    rolled_back and restored.digest == record.source_manifest_hash
                )
                error = (
                    "interrupted publication was rolled back during startup"
                    if not manual
                    else "interrupted publication could not be safely rolled back"
                )
            except Exception:
                manual = True
            try:
                self.store.fail_publication(
                    record.run_id,
                    request_hash=record.request_hash,
                    error=error,
                    manual_recovery_required=manual,
                )
            except Exception:
                # Startup stays available for inspection even if the durable
                # recovery status itself cannot be updated.
                pass

    def _publication_runner_requests(
        self, run: RunRecord, profile: FrozenProjectProfile
    ) -> dict[str, RunnerRequest]:
        output_dir = (
            self.runtime_root / "runs" / run.run_id / "publication" / "test-output"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        project_root = Path(__file__).resolve().parents[1]
        baseline = project_root / "tests" / "golden" / "baseline"
        if run.acceptance_pack_id is not None:
            if self._acceptance_registry is None:
                raise RuntimeConfigurationError("acceptance pack registry is unavailable")
            pack = self._acceptance_registry.get(run.acceptance_pack_id)
            if pack.test_dir is None:
                raise RuntimeConfigurationError("acceptance pack is not runnable")
            acceptance = pack.test_dir
            if (
                pack.runner_image != profile.runner_image
                or pack.runner_digest != profile.runner_digest
                or pack.command != profile.commands.acceptance_test
            ):
                raise RuntimeConfigurationError(
                    "acceptance pack runner identity does not match the execution profile"
                )
        else:
            acceptance = project_root / "tests" / "golden" / "acceptance"
        return {
            command_id: RunnerRequest(
                run_id=f"{run.run_id}-publish",
                lease_generation=1,
                command_id=command_id,
                workspace=profile.source_path,
                baseline_tests=baseline,
                acceptance_tests=acceptance,
                output_dir=output_dir,
            )
            for command_id in ("baseline_test", "acceptance_test")
        }

    def _runner_requests(
        self, run: RunRecord, token: LeaseToken, snapshot: WorkspaceSnapshot
    ) -> dict[str, RunnerRequest]:
        output_dir = (
            self.runtime_root
            / "runs"
            / run.run_id
            / "attempts"
            / f"g{token.generation}"
            / "test-output"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        project_root = Path(__file__).resolve().parents[1]
        baseline = project_root / "tests" / "golden" / "baseline"
        if run.acceptance_pack_id is not None:
            if self._acceptance_registry is None:
                raise RuntimeConfigurationError("acceptance pack registry is unavailable")
            pack = self._acceptance_registry.get(run.acceptance_pack_id)
            acceptance = pack.test_dir
            if acceptance is None:
                raise RuntimeConfigurationError("acceptance pack is not runnable")
            profile = self._verified_profile(run)
            if (
                pack.runner_image != profile.runner_image
                or pack.runner_digest != profile.runner_digest
                or pack.command != profile.commands.acceptance_test
            ):
                raise RuntimeConfigurationError(
                    "acceptance pack runner identity does not match the execution profile"
                )
        else:
            acceptance = project_root / "tests" / "golden" / "acceptance"
        return {
            command_id: RunnerRequest(
                run_id=run.run_id,
                lease_generation=token.generation,
                command_id=command_id,
                workspace=snapshot.workspace_root,
                baseline_tests=baseline,
                acceptance_tests=acceptance,
                output_dir=output_dir,
            )
            for command_id in ("baseline_test", "acceptance_test")
        }

    def _persist_result(self, token: LeaseToken, result: Mapping[str, Any]) -> None:
        diff = str(result.get("diff", ""))
        report = {
            "run_id": token.run_id,
            "status": str(result["status"]),
            "policy_passed": bool(result.get("policy_passed")),
            "policy_violations": list(result.get("policy_violations", [])),
            "changed_files": list(result.get("changed_files", [])),
            "baseline_test_result": _model_dump(result.get("baseline_test_result")),
            "acceptance_test_result": _model_dump(result.get("acceptance_test_result")),
            "review": _model_dump(result.get("review")),
            "final_result": _model_dump(result.get("final_result")),
        }
        sink = _FencedArtifactSink(
            runtime_root=self.runtime_root, store=self.store, token=token
        )
        sink.write_text(run_id=token.run_id, name="change.patch", content=diff)
        sink.write_text(
            run_id=token.run_id,
            name="report.json",
            content=json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        )

    def _verified_profile(self, run: RunRecord) -> FrozenProjectProfile:
        profile = self._load_profile(run.project_id)
        if profile.profile_hash != run.project_profile_hash:
            raise RuntimeConfigurationError(
                "project profile changed after run creation; create a new run"
            )
        return profile

    def _llm_executor(self, token: LeaseToken) -> FencedLLMCallExecutor:
        return FencedLLMCallExecutor(
            provider=self._provider,
            store=self.store,
            token=token,
            settings=self._llm_settings,
        )

    def _source_snapshot_path(self, run_id: str) -> Path:
        return self.runtime_root / "runs" / run_id / "source"

    @staticmethod
    def _context_profile(
        profile: FrozenProjectProfile, source_root: Path
    ) -> FrozenProjectProfile:
        # Context indexing needs the run snapshot as its physical root.  Its
        # profile hash is independent from the approval-bound authorization
        # hash and never substitutes for it.
        from app.config import ProjectProfile, freeze_project_profile

        payload = profile.model_dump(mode="python", exclude={"profile_hash"})
        payload["source_path"] = source_root
        return freeze_project_profile(ProjectProfile.model_validate(payload))

    def _resolve_acceptance_pack(
        self,
        pack_id: str | None,
        *,
        project_id: str,
        requirement: str,
    ) -> AcceptancePack | None:
        if pack_id is None:
            return None
        if self._acceptance_registry is None:
            raise RuntimeConfigurationError("acceptance pack registry is unavailable")
        return self._acceptance_registry.resolve_for_run(
            pack_id, project_id=project_id, requirement=requirement
        )

    def _assert_acceptance_pack_unchanged(self, run: RunRecord) -> None:
        if run.acceptance_pack_id is None:
            if self._provider_mode != ProviderMode.MOCK.value:
                raise RuntimeConfigurationError("real LLM run has no acceptance pack")
            return
        if self._acceptance_registry is None:
            raise RuntimeConfigurationError("acceptance pack registry is unavailable")
        pack = self._acceptance_registry.get(run.acceptance_pack_id)
        if not self._acceptance_registry.matches_bound_hash(
            run.acceptance_pack_id,
            run.acceptance_pack_hash,
            bound_at=run.created_at.timestamp(),
        ):
            raise RuntimeConfigurationError("acceptance pack changed after run creation")
        self._acceptance_registry.assert_unchanged(pack)

    @staticmethod
    def _validate_approval_bindings(
        run: RunRecord, approval: ApprovalRecord
    ) -> None:
        expected = (
            ("source manifest", run.source_manifest_hash, approval.source_manifest_hash),
            ("context bundle", run.context_bundle_hash, approval.context_bundle_hash),
            ("acceptance pack", run.acceptance_pack_hash, approval.acceptance_pack_hash),
        )
        for label, run_value, approved_value in expected:
            if run_value != approved_value:
                raise RuntimeError(f"approval no longer matches the bound {label}")

    def _load_profile(self, project_id: str) -> FrozenProjectProfile:
        try:
            path = self._profile_paths[project_id]
        except KeyError as exc:
            raise RuntimeConfigurationError(f"unknown project_id: {project_id}") from exc
        profile = load_frozen_project_profile(path)
        if profile.project_id != project_id:
            raise RuntimeConfigurationError(
                f"profile id {profile.project_id!r} does not match registry key {project_id!r}"
            )
        return profile

    @staticmethod
    def _graph_config(run_id: str, generation: int) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": f"{run_id}:g{generation}"}}


def _read_manifest_files(root: Path, manifest) -> dict[str, str]:
    return {
        entry.path: (root / Path(entry.path)).read_bytes().decode("utf-8")
        for entry in manifest.entries
    }


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Durably replace one file using a temporary sibling."""

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_selected_files(
    root: Path, paths, policy: EffectiveWorkspacePolicy
) -> dict[str, str]:
    selected: dict[str, str] = {}
    for raw in sorted(set(paths), key=str.casefold):
        normalized = policy.require_allowed(raw)
        path = root / Path(normalized)
        if path.is_file():
            # Decode raw bytes directly so CRLF remains part of the approved
            # base hash on Windows instead of being normalized by TextIO.
            selected[normalized] = path.read_bytes().decode("utf-8")
    return selected


def _secret_findings(diff: str) -> list[str]:
    findings: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++") and _POSSIBLE_SECRET.search(line):
            findings.append("possible secret in added line")
            break
    return findings


def _model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def _safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}"[:1_000]
