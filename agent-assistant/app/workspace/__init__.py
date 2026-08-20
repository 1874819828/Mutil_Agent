"""Secure workspace creation, policy, manifests, and real filesystem diffs."""

from .diff import WorkspaceDiff, generate_unified_diff
from .errors import (
    CaseCollisionError,
    InvalidRelativePathError,
    OriginalProjectModifiedError,
    PathNotAllowedError,
    QuotaExceededError,
    SensitivePathError,
    UnsafeFileContentError,
    UnsafeFilesystemEntryError,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceSecurityError,
)
from .manifest import ManifestEntry, WorkspaceManifest, build_manifest
from .paths import normalize_relative_path, resolve_within
from .policy import EffectiveWorkspacePolicy, build_effective_policy
from .service import (
    WorkspaceService,
    WorkspaceSnapshot,
    create_workspace,
    validate_write_path,
)

__all__ = [
    "CaseCollisionError",
    "EffectiveWorkspacePolicy",
    "InvalidRelativePathError",
    "ManifestEntry",
    "OriginalProjectModifiedError",
    "PathNotAllowedError",
    "QuotaExceededError",
    "SensitivePathError",
    "UnsafeFileContentError",
    "UnsafeFilesystemEntryError",
    "WorkspaceDiff",
    "WorkspaceError",
    "WorkspaceExistsError",
    "WorkspaceManifest",
    "WorkspaceSecurityError",
    "WorkspaceService",
    "WorkspaceSnapshot",
    "build_effective_policy",
    "build_manifest",
    "create_workspace",
    "generate_unified_diff",
    "normalize_relative_path",
    "resolve_within",
    "validate_write_path",
]
