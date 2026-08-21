"""Frozen project permissions and exact Manager-request intersection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Sequence

from app.config.project_profiles import FrozenProjectProfile, freeze_project_profile

from .errors import InvalidRelativePathError, PathNotAllowedError, SensitivePathError
from .globs import matches_any
from .paths import normalize_relative_path


_DENIED_COMPONENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
    }
)
_DENIED_SUFFIXES = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
        ".der",
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".bin",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".pdf",
    }
)
_DENIED_EXACT_FILES = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "secrets.json",
        ".assistant-workspace.json",
    }
)


def is_globally_sensitive(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = tuple(part.casefold() for part in normalized.split("/"))
    if any(part in _DENIED_COMPONENTS for part in parts):
        return True
    name = parts[-1]
    if name == ".env" or name.startswith(".env.") or name in _DENIED_EXACT_FILES:
        return True
    if name.startswith("credentials.") or name.startswith("secret."):
        return True
    return any(name.endswith(suffix) for suffix in _DENIED_SUFFIXES)


def _normalize_requested_glob(value: str) -> str:
    value = value.strip().replace("\\", "/")
    windows = PureWindowsPath(value)
    if (
        not value
        or "\x00" in value
        or ":" in value
        or value.startswith("/")
        or windows.is_absolute()
        or windows.drive
    ):
        raise InvalidRelativePathError("requested glob must be a portable relative pattern")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise InvalidRelativePathError("requested glob traversal is forbidden")
    return value


@dataclass(frozen=True, slots=True)
class EffectiveWorkspacePolicy:
    """A conjunctive policy; a path must match both independent allowlists."""

    profile_hash: str
    profile_include_globs: tuple[str, ...]
    profile_exclude_globs: tuple[str, ...]
    requested_globs: tuple[str, ...]

    @property
    def effective_globs(self) -> tuple[tuple[str, str], ...]:
        """Auditable symbolic intersections; enforcement always uses ``allows``."""

        return tuple(
            (profile_glob, requested_glob)
            for profile_glob in self.profile_include_globs
            for requested_glob in self.requested_globs
        )

    def allows(self, relative_path: str) -> bool:
        try:
            normalized = normalize_relative_path(relative_path)
        except InvalidRelativePathError:
            return False
        if is_globally_sensitive(normalized):
            return False
        if self.profile_exclude_globs and matches_any(
            normalized, self.profile_exclude_globs
        ):
            return False
        return matches_any(normalized, self.profile_include_globs) and matches_any(
            normalized, self.requested_globs
        )

    def require_allowed(self, relative_path: str) -> str:
        normalized = normalize_relative_path(relative_path)
        if is_globally_sensitive(normalized):
            raise SensitivePathError(f"globally denied sensitive path: {normalized}")
        if not self.allows(normalized):
            raise PathNotAllowedError(f"path is outside effective policy: {normalized}")
        return normalized


def build_effective_policy(
    profile: FrozenProjectProfile, requested_globs: Sequence[str] | None = None
) -> EffectiveWorkspacePolicy:
    frozen = freeze_project_profile(profile)
    requested = (
        frozen.include_globs
        if requested_globs is None
        else tuple(_normalize_requested_glob(item) for item in requested_globs)
    )
    if not requested:
        raise ValueError("effective policy requires at least one requested glob")
    if len({item.casefold() for item in requested}) != len(requested):
        raise ValueError("requested globs must be unique")
    return EffectiveWorkspacePolicy(
        profile_hash=frozen.profile_hash,
        profile_include_globs=frozen.include_globs,
        profile_exclude_globs=frozen.exclude_globs,
        requested_globs=requested,
    )

