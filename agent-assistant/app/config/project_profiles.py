"""Project profile loading and deterministic approval-bound freezing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _normalize_glob(value: str) -> str:
    value = value.strip().replace("\\", "/")
    windows = PureWindowsPath(value)
    if (
        not value
        or "\x00" in value
        or ":" in value
        or value.startswith("/")
        or windows.is_absolute()
        or bool(windows.drive)
    ):
        raise ValueError("glob must be a non-empty portable relative pattern")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("glob traversal and non-canonical components are forbidden")
    return "/".join(parts)


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ProjectCommands(ProfileModel):
    """Fixed argv commands; shell command strings are intentionally invalid."""

    syntax: tuple[str, ...] = Field(min_length=1)
    baseline_test: tuple[str, ...] = Field(min_length=1)
    acceptance_test: tuple[str, ...] = Field(min_length=1)
    developer_test: tuple[str, ...] | None = None

    @field_validator("syntax", "baseline_test", "acceptance_test", "developer_test")
    @classmethod
    def argv_is_safe(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if any(not token.strip() or "\x00" in token for token in value):
            raise ValueError("command argv tokens must be non-empty and cannot contain NUL")
        return value

    def by_id(self, command_id: str) -> tuple[str, ...]:
        value = getattr(self, command_id, None)
        if value is None:
            raise KeyError(f"unknown or disabled command id: {command_id}")
        return value


class ProjectLimits(ProfileModel):
    timeout_seconds: int = Field(gt=0, le=3600)
    memory_mb: int = Field(gt=0, le=32768)
    cpus: float = Field(gt=0, le=64)
    max_files: int = Field(gt=0, le=1_000_000)
    max_file_mb: float = Field(gt=0, le=4096)
    max_workspace_mb: float = Field(gt=0, le=1_000_000)

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    @property
    def max_workspace_bytes(self) -> int:
        return int(self.max_workspace_mb * 1024 * 1024)


class ProjectProfile(ProfileModel):
    project_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    source_path: Path
    include_globs: tuple[str, ...] = Field(min_length=1)
    exclude_globs: tuple[str, ...] = ()
    runner: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    commands: ProjectCommands
    runner_image: str = Field(min_length=1)
    runner_digest: str | None = None
    limits: ProjectLimits

    @field_validator("include_globs", "exclude_globs")
    @classmethod
    def globs_are_safe_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_glob(item) for item in value)
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("glob patterns must be unique under case-insensitive comparison")
        return normalized

    @model_validator(mode="after")
    def workspace_limits_are_consistent(self) -> ProjectProfile:
        if self.limits.max_file_bytes > self.limits.max_workspace_bytes:
            raise ValueError("max_file_mb cannot exceed max_workspace_mb")
        return self


class FrozenProjectProfile(ProjectProfile):
    """A semantic snapshot whose hash is bound to approvals and run state."""

    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _profile_hash_payload(profile: ProjectProfile) -> dict[str, Any]:
    payload = profile.model_dump(mode="json", exclude={"profile_hash"}, exclude_none=False)
    payload["source_path"] = profile.source_path.resolve(strict=False).as_posix()
    return payload


def _canonical_profile_hash(profile: ProjectProfile) -> str:
    encoded = json.dumps(
        _profile_hash_payload(profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_project_profile(profile: ProjectProfile) -> FrozenProjectProfile:
    if isinstance(profile, FrozenProjectProfile):
        expected = _canonical_profile_hash(profile)
        if profile.profile_hash != expected:
            raise ValueError("frozen project profile hash does not match its content")
        return profile
    payload = profile.model_dump(mode="python")
    payload["source_path"] = profile.source_path.resolve(strict=False)
    payload["profile_hash"] = _canonical_profile_hash(profile)
    return FrozenProjectProfile.model_validate(payload)


def load_project_profile(path: str | Path) -> ProjectProfile:
    profile_path = Path(path).resolve(strict=True)
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load project profile {profile_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("project profile root must be a YAML mapping")
    source_path = raw.get("source_path")
    if isinstance(source_path, str):
        candidate = Path(source_path)
        if not candidate.is_absolute():
            candidate = profile_path.parent / candidate
        raw["source_path"] = candidate.resolve(strict=False)
    return ProjectProfile.model_validate(raw)


def load_frozen_project_profile(path: str | Path) -> FrozenProjectProfile:
    return freeze_project_profile(load_project_profile(path))

