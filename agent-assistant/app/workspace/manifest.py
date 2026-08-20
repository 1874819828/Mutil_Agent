"""Filesystem-derived, quota-bounded workspace manifests."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.config.project_profiles import ProjectLimits

from .errors import (
    CaseCollisionError,
    PathNotAllowedError,
    QuotaExceededError,
    SensitivePathError,
    UnsafeFileContentError,
    UnsafeFilesystemEntryError,
)
from .paths import canonical_root, is_reparse_point
from .policy import EffectiveWorkspacePolicy, is_globally_sensitive


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    entries: tuple[ManifestEntry, ...]
    total_bytes: int
    digest: str
    skipped_paths: tuple[str, ...] = ()

    @property
    def by_path(self) -> dict[str, ManifestEntry]:
        return {entry.path: entry for entry in self.entries}


def _manifest_digest(entries: tuple[ManifestEntry, ...]) -> str:
    payload = [
        {"path": entry.path, "sha256": entry.sha256, "size": entry.size}
        for entry in entries
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _scan_selected_files(
    root: Path,
    policy: EffectiveWorkspacePolicy,
    limits: ProjectLimits,
    *,
    reject_unallowed: bool,
) -> tuple[tuple[ManifestEntry, ...], tuple[str, ...]]:
    root = canonical_root(root)
    entries: list[ManifestEntry] = []
    skipped: list[str] = []
    casefold_paths: dict[str, str] = {}
    total_bytes = 0
    pending = [root]

    while pending:
        directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise UnsafeFilesystemEntryError(f"cannot scan directory: {directory}") from exc
        for child in children:
            child_path = Path(child.path)
            relative = child_path.relative_to(root).as_posix()
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise UnsafeFilesystemEntryError(f"cannot inspect entry: {relative}") from exc
            attributes = getattr(child_stat, "st_file_attributes", 0)
            if child.is_symlink() or is_reparse_point(child_path) or attributes & 0x400:
                if not is_globally_sensitive(relative):
                    raise UnsafeFilesystemEntryError(
                        f"symbolic link or reparse point is forbidden: {relative}"
                    )
                skipped.append(relative)
                continue
            if child.is_dir(follow_symlinks=False):
                if is_globally_sensitive(relative):
                    skipped.append(relative + "/")
                    continue
                pending.append(child_path)
                continue
            if not child.is_file(follow_symlinks=False):
                raise UnsafeFilesystemEntryError(
                    f"non-regular filesystem entry is forbidden: {relative}"
                )
            if not policy.allows(relative):
                if is_globally_sensitive(relative):
                    skipped.append(relative)
                    if reject_unallowed and relative != ".assistant-workspace.json":
                        raise SensitivePathError(
                            f"globally denied file exists in workspace: {relative}"
                        )
                elif reject_unallowed:
                    raise PathNotAllowedError(
                        f"file exists outside effective workspace policy: {relative}"
                    )
                continue
            size = child_stat.st_size
            if size > limits.max_file_bytes:
                raise QuotaExceededError(
                    f"file exceeds max_file_mb: {relative} ({size} bytes)"
                )
            if len(entries) + 1 > limits.max_files:
                raise QuotaExceededError("workspace exceeds max_files")
            total_bytes += size
            if total_bytes > limits.max_workspace_bytes:
                raise QuotaExceededError("workspace exceeds max_workspace_mb")
            folded = relative.casefold()
            if folded in casefold_paths and casefold_paths[folded] != relative:
                raise CaseCollisionError(
                    "case-insensitive path collision: "
                    f"{casefold_paths[folded]} and {relative}"
                )
            casefold_paths[folded] = relative
            try:
                data = child_path.read_bytes()
            except OSError as exc:
                raise UnsafeFilesystemEntryError(f"cannot read selected file: {relative}") from exc
            if len(data) != size:
                raise UnsafeFilesystemEntryError(f"file changed while scanning: {relative}")
            if not _is_text(data):
                raise UnsafeFileContentError(
                    f"selected file is not UTF-8 text: {relative}"
                )
            entries.append(
                ManifestEntry(
                    path=relative,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size=size,
                )
            )
    entries_tuple = tuple(sorted(entries, key=lambda item: item.path.casefold()))
    return entries_tuple, tuple(sorted(skipped, key=str.casefold))


def build_manifest(
    root: Path,
    policy: EffectiveWorkspacePolicy,
    limits: ProjectLimits,
    *,
    reject_unallowed: bool = False,
) -> WorkspaceManifest:
    entries, skipped = _scan_selected_files(
        root, policy, limits, reject_unallowed=reject_unallowed
    )
    return WorkspaceManifest(
        entries=entries,
        total_bytes=sum(entry.size for entry in entries),
        digest=_manifest_digest(entries),
        skipped_paths=skipped,
    )
