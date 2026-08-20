"""Unified diffs derived from actual baseline and workspace files."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.config.project_profiles import ProjectLimits

from .errors import PathNotAllowedError
from .manifest import build_manifest
from .paths import resolve_within
from .policy import EffectiveWorkspacePolicy


@dataclass(frozen=True, slots=True)
class WorkspaceDiff:
    text: str
    changed_files: tuple[str, ...]
    added_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    sha256: str


def _read_lines(root: Path, path: str) -> list[str]:
    return resolve_within(root, path, must_exist=True).read_text(encoding="utf-8").splitlines(
        keepends=True
    )


def generate_unified_diff(
    baseline_root: Path,
    workspace_root: Path,
    policy: EffectiveWorkspacePolicy,
    limits: ProjectLimits,
) -> WorkspaceDiff:
    # Scan the whole frozen profile first. A narrow Manager plan must not make
    # unchanged copied files look illegal, but it also must not hide changes to
    # those files from the policy gate.
    profile_policy = EffectiveWorkspacePolicy(
        profile_hash=policy.profile_hash,
        profile_include_globs=policy.profile_include_globs,
        profile_exclude_globs=policy.profile_exclude_globs,
        requested_globs=policy.profile_include_globs,
    )
    baseline = build_manifest(baseline_root, profile_policy, limits)
    workspace = build_manifest(
        workspace_root, profile_policy, limits, reject_unallowed=True
    )
    baseline_by_path = baseline.by_path
    workspace_by_path = workspace.by_path
    changed = tuple(
        path
        for path in sorted(
            set(baseline_by_path) | set(workspace_by_path), key=str.casefold
        )
        if baseline_by_path.get(path) != workspace_by_path.get(path)
    )
    disallowed_changes = tuple(path for path in changed if not policy.allows(path))
    if disallowed_changes:
        raise PathNotAllowedError(
            "workspace contains changes outside effective policy: "
            + ", ".join(disallowed_changes)
        )
    added = tuple(path for path in changed if path not in baseline_by_path)
    deleted = tuple(path for path in changed if path not in workspace_by_path)
    modified = tuple(
        path for path in changed if path in baseline_by_path and path in workspace_by_path
    )

    chunks: list[str] = []
    for path in changed:
        old_lines = _read_lines(baseline_root, path) if path in baseline_by_path else []
        new_lines = _read_lines(workspace_root, path) if path in workspace_by_path else []
        for line in difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}" if old_lines else "/dev/null",
            tofile=f"b/{path}" if new_lines else "/dev/null",
            lineterm="\n",
        ):
            # ``difflib`` preserves a missing EOF newline in hunk body lines.
            # Joining those raw strings would concatenate the next body line or
            # file header and create a corrupt multi-file patch.  Emit the
            # conventional marker so git/patch can apply it without changing
            # the target file's EOF semantics.
            if not line.endswith(("\n", "\r")):
                chunks.append(line + "\n\\ No newline at end of file\n")
            else:
                chunks.append(line)
    text = "".join(chunks)
    return WorkspaceDiff(
        text=text,
        changed_files=changed,
        added_files=added,
        modified_files=modified,
        deleted_files=deleted,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
