"""Transactional creation of isolated, allowlist-only workspaces."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config.project_profiles import (
    FrozenProjectProfile,
    ProjectLimits,
    freeze_project_profile,
)

from .errors import (
    OriginalProjectModifiedError,
    UnsafeFilesystemEntryError,
    WorkspaceExistsError,
)
from .manifest import WorkspaceManifest, build_manifest
from .paths import canonical_root, reject_reparse_ancestors, resolve_within, roots_overlap
from .policy import EffectiveWorkspacePolicy, build_effective_policy


WORKSPACE_MARKER = ".assistant-workspace.json"
MARKER_VERSION = 1


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    source_root: Path
    workspace_root: Path
    profile_hash: str
    source_manifest: WorkspaceManifest
    workspace_manifest: WorkspaceManifest
    skipped_paths: tuple[str, ...]
    policy: EffectiveWorkspacePolicy
    limits: ProjectLimits


class WorkspaceService:
    def create_workspace(
        self,
        profile: FrozenProjectProfile,
        destination: str | Path,
    ) -> WorkspaceSnapshot:
        frozen = freeze_project_profile(profile)
        source = canonical_root(frozen.source_path)
        destination_path = Path(destination).absolute()
        reject_reparse_ancestors(destination_path.parent)
        if roots_overlap(source, destination_path):
            raise UnsafeFilesystemEntryError(
                "source and workspace roots must be completely separate"
            )
        policy = build_effective_policy(frozen)
        source_manifest = build_manifest(source, policy, frozen.limits)

        return self._copy_manifest(
            frozen=frozen,
            source=source,
            destination_path=destination_path,
            policy=policy,
            source_manifest=source_manifest,
        )

    def create_workspace_from_snapshot(
        self,
        profile: FrozenProjectProfile,
        source_snapshot: str | Path,
        destination: str | Path,
        *,
        expected_manifest_hash: str,
    ) -> WorkspaceSnapshot:
        """Create a generation workspace only from the run's frozen source."""

        frozen = freeze_project_profile(profile)
        source = canonical_root(source_snapshot)
        destination_path = Path(destination).absolute()
        reject_reparse_ancestors(destination_path.parent)
        if roots_overlap(source, destination_path):
            raise UnsafeFilesystemEntryError(
                "source snapshot and generation workspace must be separate"
            )
        policy = build_effective_policy(frozen)
        source_manifest = build_manifest(source, policy, frozen.limits)
        if source_manifest.digest != expected_manifest_hash:
            raise OriginalProjectModifiedError(
                "the immutable run source snapshot no longer matches its bound digest"
            )
        return self._copy_manifest(
            frozen=frozen,
            source=source,
            destination_path=destination_path,
            policy=policy,
            source_manifest=source_manifest,
        )

    def _copy_manifest(
        self,
        *,
        frozen: FrozenProjectProfile,
        source: Path,
        destination_path: Path,
        policy: EffectiveWorkspacePolicy,
        source_manifest: WorkspaceManifest,
    ) -> WorkspaceSnapshot:

        if destination_path.exists() or destination_path.is_symlink():
            return self._load_idempotent(
                frozen, source, destination_path, policy, source_manifest
            )

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        reject_reparse_ancestors(destination_path.parent)
        staging = destination_path.with_name(
            f".{destination_path.name}.tmp-{uuid.uuid4().hex}"
        )
        try:
            staging.mkdir()
            for entry in source_manifest.entries:
                source_file = resolve_within(source, entry.path, must_exist=True)
                data = source_file.read_bytes()
                if len(data) != entry.size:
                    raise UnsafeFilesystemEntryError(
                        f"source file changed while copying: {entry.path}"
                    )
                import hashlib

                if hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise UnsafeFilesystemEntryError(
                        f"source file changed while copying: {entry.path}"
                    )
                target = staging.joinpath(*entry.path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

            workspace_manifest = build_manifest(
                staging, policy, frozen.limits, reject_unallowed=True
            )
            if workspace_manifest.digest != source_manifest.digest:
                raise UnsafeFilesystemEntryError(
                    "workspace manifest does not match the source snapshot"
                )
            marker = {
                "version": MARKER_VERSION,
                "project_id": frozen.project_id,
                "profile_hash": frozen.profile_hash,
                "source_root": source.as_posix(),
                "source_manifest_digest": source_manifest.digest,
                "workspace_manifest_digest": workspace_manifest.digest,
            }
            (staging / WORKSPACE_MARKER).write_text(
                json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(staging, destination_path)
        except Exception:
            if staging.exists() and staging.parent == destination_path.parent:
                shutil.rmtree(staging)
            raise

        final_manifest = build_manifest(
            destination_path, policy, frozen.limits, reject_unallowed=True
        )
        if final_manifest.digest != source_manifest.digest:
            raise UnsafeFilesystemEntryError(
                "workspace changed during final post-copy verification"
            )
        return WorkspaceSnapshot(
            source_root=source,
            workspace_root=destination_path.resolve(strict=True),
            profile_hash=frozen.profile_hash,
            source_manifest=source_manifest,
            workspace_manifest=final_manifest,
            skipped_paths=source_manifest.skipped_paths,
            policy=policy,
            limits=frozen.limits,
        )

    def _load_idempotent(
        self,
        frozen: FrozenProjectProfile,
        source: Path,
        destination: Path,
        policy: EffectiveWorkspacePolicy,
        source_manifest: WorkspaceManifest,
    ) -> WorkspaceSnapshot:
        try:
            destination = canonical_root(destination)
            marker_path = resolve_within(destination, WORKSPACE_MARKER, must_exist=True)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise WorkspaceExistsError(
                "workspace destination exists without a valid marker"
            ) from exc
        expected = {
            "version": MARKER_VERSION,
            "project_id": frozen.project_id,
            "profile_hash": frozen.profile_hash,
            "source_root": source.as_posix(),
            "source_manifest_digest": source_manifest.digest,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            raise WorkspaceExistsError(
                "workspace marker belongs to a different source/profile snapshot"
            )
        manifest = build_manifest(
            destination, policy, frozen.limits, reject_unallowed=True
        )
        if marker.get("workspace_manifest_digest") != manifest.digest:
            raise WorkspaceExistsError("existing workspace is no longer pristine")
        return WorkspaceSnapshot(
            source_root=source,
            workspace_root=destination,
            profile_hash=frozen.profile_hash,
            source_manifest=source_manifest,
            workspace_manifest=manifest,
            skipped_paths=source_manifest.skipped_paths,
            policy=policy,
            limits=frozen.limits,
        )

    def assert_source_unchanged(self, snapshot: WorkspaceSnapshot) -> None:
        current = build_manifest(snapshot.source_root, snapshot.policy, snapshot.limits)
        if current.entries != snapshot.source_manifest.entries:
            raise OriginalProjectModifiedError(
                "the original project changed after workspace creation"
            )


def create_workspace(
    profile: FrozenProjectProfile, destination: str | Path
) -> WorkspaceSnapshot:
    return WorkspaceService().create_workspace(profile, destination)


def validate_write_path(
    workspace_root: str | Path,
    relative_path: str,
    policy: EffectiveWorkspacePolicy,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a write target only after both policy and canonical-path checks."""

    normalized = policy.require_allowed(relative_path)
    return resolve_within(Path(workspace_root), normalized, must_exist=must_exist)
