"""External API request contracts.

Internal workflow contracts live under :mod:`app.contracts`; these models are
kept deliberately small so untrusted HTTP input cannot inject runtime paths or
execution commands.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CreateRunRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    requirement: str = Field(min_length=8, max_length=10_000)
    acceptance_pack_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )

    @field_validator("requirement")
    @classmethod
    def strip_requirement(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 8:
            raise ValueError("requirement is too short after trimming")
        return value


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject"]
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    project_profile_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    source_manifest_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    context_bundle_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    acceptance_pack_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    comment: str | None = Field(default=None, max_length=2_000)


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1_000)


class PublishRunRequest(BaseModel):
    confirmation: Literal["publish"]
    project_profile_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    source_manifest_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    diff_sha256: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    git_original_branch: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    git_base_commit: str = Field(
        min_length=40,
        max_length=64,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )
    git_target_branch: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    git_remote_name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    git_remote_url: str = Field(min_length=1, max_length=2048)
    idempotency_key: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    comment: str | None = Field(default=None, max_length=2_000)

    @field_validator("git_remote_url")
    @classmethod
    def remote_url_has_no_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Git remote URL contains control characters")
        return value
