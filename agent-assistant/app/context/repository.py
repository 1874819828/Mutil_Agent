"""Build the small, auditable repository context sent to an LLM.

The service treats model-selected paths as hostile input.  A path is useful only
when it is an exact UTF-8 text file selected by the frozen project profile and
still has the hash recorded in the local index.  Globs are deliberately not
accepted here: a model cannot use ``**/*`` to turn an inspection request into a
permission expansion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.config.project_profiles import FrozenProjectProfile, freeze_project_profile
from app.workspace.errors import InvalidRelativePathError
from app.workspace.paths import canonical_root, is_reparse_point, resolve_within
from app.workspace.policy import build_effective_policy, is_globally_sensitive


class RepositoryContextError(RuntimeError):
    """Base class for fail-closed context errors."""


class UnsafeContextPathError(RepositoryContextError, ValueError):
    pass


class UnsafeContextContentError(RepositoryContextError, ValueError):
    pass


class ContextBudgetExceededError(RepositoryContextError):
    pass


class RepositoryChangedError(RepositoryContextError):
    pass


_GLOB_META = frozenset("*?[")
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("OpenAI-style API key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    (
        "assigned credential",
        re.compile(
            rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
            rb"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"
        ),
    ),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_hash(value: object) -> str:
    return _sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _secret_reason(data: bytes) -> str | None:
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(data):
            return label
    return None


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise UnsafeContextContentError("binary content is forbidden in LLM context")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsafeContextContentError("non-UTF-8 content is forbidden in LLM context") from exc


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Independent LLM-context quotas, intentionally below workspace quotas."""

    max_files_per_round: int = 20
    round_byte_limits: tuple[int, int] = (64 * 1024, 128 * 1024)
    max_total_files: int = 30
    max_total_bytes: int = 128 * 1024

    def __post_init__(self) -> None:
        if self.max_files_per_round <= 0 or self.max_total_files <= 0:
            raise ValueError("context file budgets must be positive")
        if len(self.round_byte_limits) != 2 or any(
            limit <= 0 for limit in self.round_byte_limits
        ):
            raise ValueError("exactly two positive round byte limits are required")
        if self.max_total_bytes <= 0:
            raise ValueError("context byte budget must be positive")


@dataclass(frozen=True, slots=True)
class InspectionRequest:
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.paths:
            raise ValueError("inspection request requires at least one path")
        if any(not isinstance(path, str) or not path.strip() for path in self.paths):
            raise ValueError("inspection paths must be non-empty strings")
        if len({path.strip().replace("\\", "/").casefold() for path in self.paths}) != len(
            self.paths
        ):
            raise ValueError("inspection paths must be unique")


@dataclass(frozen=True, slots=True)
class RepositoryIndexEntry:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class RepositoryIndex:
    root: Path
    profile_hash: str
    entries: tuple[RepositoryIndexEntry, ...]
    rejected_paths: tuple[str, ...]
    digest: str

    @property
    def by_path(self) -> dict[str, RepositoryIndexEntry]:
        return {entry.path.casefold(): entry for entry in self.entries}


@dataclass(frozen=True, slots=True)
class ContextFile:
    path: str
    sha256: str
    size: int
    content: str


@dataclass(frozen=True, slots=True)
class ContextBundle:
    round_number: int
    source_manifest_hash: str
    files: tuple[ContextFile, ...]
    total_bytes: int
    bundle_hash: str


class RepositoryContextService:
    def __init__(
        self,
        profile: FrozenProjectProfile,
        *,
        budget: ContextBudget | None = None,
    ) -> None:
        self.profile = freeze_project_profile(profile)
        self.root = canonical_root(self.profile.source_path)
        self.policy = build_effective_policy(self.profile)
        self.budget = budget or ContextBudget()

    def build_index(self) -> RepositoryIndex:
        entries: list[RepositoryIndexEntry] = []
        rejected: list[str] = []
        casefold_seen: dict[str, str] = {}
        pending = [self.root]
        max_context_file_bytes = max(self.budget.round_byte_limits)
        indexed_bytes = 0

        while pending:
            directory = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise UnsafeContextPathError(f"cannot scan repository directory: {directory}") from exc
            for child in children:
                path = Path(child.path)
                relative = path.relative_to(self.root).as_posix()
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise UnsafeContextPathError(f"cannot inspect repository path: {relative}") from exc
                attributes = getattr(metadata, "st_file_attributes", 0)
                if child.is_symlink() or is_reparse_point(path) or attributes & 0x400:
                    raise UnsafeContextPathError(
                        f"symbolic link or reparse point is forbidden: {relative}"
                    )
                if child.is_dir(follow_symlinks=False):
                    if is_globally_sensitive(relative):
                        rejected.append(relative + "/")
                    else:
                        pending.append(path)
                    continue
                if not child.is_file(follow_symlinks=False):
                    raise UnsafeContextPathError(f"non-regular repository entry: {relative}")
                if is_globally_sensitive(relative):
                    rejected.append(relative)
                    continue
                if not self.policy.allows(relative):
                    continue
                folded = relative.casefold()
                prior = casefold_seen.get(folded)
                if prior is not None and prior != relative:
                    raise UnsafeContextPathError(
                        f"case-insensitive repository path collision: {prior} and {relative}"
                    )
                casefold_seen[folded] = relative
                if metadata.st_size > self.profile.limits.max_file_bytes or metadata.st_size > max_context_file_bytes:
                    rejected.append(relative)
                    continue
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    raise UnsafeContextPathError(f"cannot read repository file: {relative}") from exc
                if len(data) != metadata.st_size:
                    raise RepositoryChangedError(f"repository file changed while indexing: {relative}")
                try:
                    _decode_text(data)
                except UnsafeContextContentError:
                    rejected.append(relative)
                    continue
                if _secret_reason(data) is not None:
                    rejected.append(relative)
                    continue
                if len(entries) + 1 > self.profile.limits.max_files:
                    raise ContextBudgetExceededError("repository index exceeds profile max_files")
                indexed_bytes += len(data)
                if indexed_bytes > self.profile.limits.max_workspace_bytes:
                    raise ContextBudgetExceededError(
                        "repository index exceeds profile workspace byte budget"
                    )
                entries.append(RepositoryIndexEntry(relative, _sha256(data), len(data)))

        ordered = tuple(sorted(entries, key=lambda entry: entry.path.casefold()))
        digest = _canonical_hash(
            {
                "profile_hash": self.profile.profile_hash,
                "entries": [
                    {"path": entry.path, "sha256": entry.sha256, "size": entry.size}
                    for entry in ordered
                ],
            }
        )
        return RepositoryIndex(
            root=self.root,
            profile_hash=self.profile.profile_hash,
            entries=ordered,
            rejected_paths=tuple(sorted(set(rejected), key=str.casefold)),
            digest=digest,
        )

    def build_bundle(
        self,
        index: RepositoryIndex,
        request: InspectionRequest,
        *,
        round_number: int,
        prior_bundles: tuple[ContextBundle, ...] = (),
    ) -> ContextBundle:
        if round_number not in (1, 2):
            raise ContextBudgetExceededError("context inspection supports exactly two rounds")
        if index.root != self.root or index.profile_hash != self.profile.profile_hash:
            raise UnsafeContextPathError("repository index belongs to another project profile")
        if (round_number == 1 and prior_bundles) or (
            round_number == 2
            and (
                len(prior_bundles) != 1
                or prior_bundles[0].round_number != 1
            )
        ):
            raise ValueError("prior context must be exactly the first-round bundle")
        if any(bundle.source_manifest_hash != index.digest for bundle in prior_bundles):
            raise UnsafeContextPathError("prior context belongs to another source snapshot")
        if len(request.paths) > self.budget.max_files_per_round:
            raise ContextBudgetExceededError("context exceeds max files per round")

        selected: list[ContextFile] = []
        by_path = index.by_path
        rejected = {path.casefold() for path in index.rejected_paths}
        prior_paths = {
            item.path.casefold()
            for bundle in prior_bundles
            for item in bundle.files
        }
        for raw in request.paths:
            normalized = self._validate_requested_path(raw)
            if normalized.casefold() in prior_paths:
                raise ContextBudgetExceededError(
                    f"file was already inspected in an earlier round: {normalized}"
                )
            if normalized.casefold() in rejected:
                raise UnsafeContextContentError(
                    f"requested file was rejected as binary, oversized, or secret: {normalized}"
                )
            entry = by_path.get(normalized.casefold())
            if entry is None:
                raise UnsafeContextPathError(
                    f"requested path is not in the approved repository index: {normalized}"
                )
            path = resolve_within(self.root, entry.path, must_exist=True)
            try:
                metadata_before = path.stat()
                data = path.read_bytes()
                metadata_after = path.stat()
            except OSError as exc:
                raise UnsafeContextPathError(f"cannot read requested file: {entry.path}") from exc
            if (
                metadata_before.st_size != metadata_after.st_size
                or metadata_before.st_mtime_ns != metadata_after.st_mtime_ns
                or len(data) != entry.size
                or _sha256(data) != entry.sha256
            ):
                raise RepositoryChangedError(f"repository file changed after indexing: {entry.path}")
            content = _decode_text(data)
            reason = _secret_reason(data)
            if reason is not None:
                raise UnsafeContextContentError(
                    f"secret pattern ({reason}) found in requested file: {entry.path}"
                )
            selected.append(ContextFile(entry.path, entry.sha256, entry.size, content))

        selected.sort(key=lambda item: item.path.casefold())
        total_bytes = sum(item.size for item in selected)
        if total_bytes > self.budget.round_byte_limits[round_number - 1]:
            raise ContextBudgetExceededError("context exceeds byte budget for this round")
        prior_files = sum(len(bundle.files) for bundle in prior_bundles)
        prior_bytes = sum(bundle.total_bytes for bundle in prior_bundles)
        if prior_files + len(selected) > self.budget.max_total_files:
            raise ContextBudgetExceededError("context exceeds total file budget")
        if prior_bytes + total_bytes > self.budget.max_total_bytes:
            raise ContextBudgetExceededError("context exceeds total byte budget")

        payload = {
            "round_number": round_number,
            "source_manifest_hash": index.digest,
            "files": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size": item.size,
                    "content": item.content,
                }
                for item in selected
            ],
        }
        return ContextBundle(
            round_number=round_number,
            source_manifest_hash=index.digest,
            files=tuple(selected),
            total_bytes=total_bytes,
            bundle_hash=_canonical_hash(payload),
        )

    def _validate_requested_path(self, raw: str) -> str:
        if any(char in raw for char in _GLOB_META):
            raise UnsafeContextPathError("inspection requests must use exact paths, not globs")
        try:
            # resolve_within provides both portable normalization and ancestor reparse checks.
            resolved = resolve_within(self.root, raw, must_exist=False)
            normalized = resolved.relative_to(self.root).as_posix()
        except (InvalidRelativePathError, ValueError, RuntimeError) as exc:
            raise UnsafeContextPathError(f"unsafe inspection path: {raw!r}") from exc
        if is_globally_sensitive(normalized):
            raise UnsafeContextPathError(f"globally denied inspection path: {normalized}")
        if not self.policy.allows(normalized):
            raise UnsafeContextPathError(f"inspection path is outside the project profile: {normalized}")
        return normalized
