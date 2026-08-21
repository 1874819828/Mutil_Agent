"""Strict Pydantic contracts exchanged by the four logical roles."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .enums import (
    CoverageStatus,
    FileOperation,
    FindingSeverity,
    ReviewOutcome,
    RiskLevel,
    RunStatus,
)


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SourceText = Annotated[str, StringConstraints(strip_whitespace=False)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _safe_relative_posix(value: str, *, allow_glob: bool = False) -> str:
    value = value.strip()
    if not value or "\x00" in value or "\\" in value or ":" in value:
        raise ValueError("path must be a non-empty portable POSIX relative path")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("absolute or drive-qualified paths are forbidden")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path traversal and non-canonical path components are forbidden")
    if not allow_glob and any(any(char in part for char in "*?[") for part in path.parts):
        raise ValueError("file paths cannot contain glob metacharacters")
    return path.as_posix()


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskStep(ContractModel):
    id: NonEmptyText
    description: NonEmptyText
    depends_on: list[NonEmptyText] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def dependencies_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("step dependencies must be unique")
        return value


class InspectionFileRequest(ContractModel):
    path: NonEmptyText
    reason: NonEmptyText

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _safe_relative_posix(value)


class InspectionRequest(ContractModel):
    files: list[InspectionFileRequest] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def paths_are_unique(self) -> InspectionRequest:
        folded = [item.path.casefold() for item in self.files]
        if len(set(folded)) != len(folded):
            raise ValueError("inspection request paths must be unique")
        return self


class TaskPlan(ContractModel):
    summary: NonEmptyText
    acceptance_criteria: list[NonEmptyText] = Field(min_length=1)
    files_to_inspect: list[NonEmptyText] = Field(min_length=1)
    allowed_change_globs: list[NonEmptyText] = Field(min_length=1)
    steps: list[TaskStep] = Field(min_length=1)
    risk_level: RiskLevel
    test_profile: NonEmptyText

    @field_validator("files_to_inspect")
    @classmethod
    def inspection_paths_are_safe(cls, value: list[str]) -> list[str]:
        normalized = [_safe_relative_posix(item) for item in value]
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("inspection paths must be unique")
        return normalized

    @field_validator("allowed_change_globs")
    @classmethod
    def change_globs_are_safe(cls, value: list[str]) -> list[str]:
        normalized = [_safe_relative_posix(item, allow_glob=True) for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed change globs must be unique")
        return normalized

    @model_validator(mode="after")
    def step_graph_is_valid(self) -> TaskPlan:
        step_ids = [step.id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("task step ids must be unique")
        known_ids = set(step_ids)
        dependencies = {step.id: set(step.depends_on) for step in self.steps}
        for step_id, required in dependencies.items():
            if step_id in required:
                raise ValueError(f"task step {step_id!r} cannot depend on itself")
            missing = required - known_ids
            if missing:
                raise ValueError(f"task step {step_id!r} has unknown dependencies: {missing}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("task step dependency graph contains a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        return self


class FileChange(ContractModel):
    operation: FileOperation
    path: NonEmptyText
    base_sha256: Sha256 | None = None
    content: SourceText
    rationale: NonEmptyText

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _safe_relative_posix(value)

    @model_validator(mode="after")
    def base_hash_matches_operation(self) -> FileChange:
        if self.operation is FileOperation.CREATE and self.base_sha256 is not None:
            raise ValueError("create changes cannot have a base_sha256")
        if self.operation is FileOperation.UPDATE and self.base_sha256 is None:
            raise ValueError("update changes require a base_sha256")
        return self


class ProposedFileChange(ContractModel):
    """LLM proposal; update hashes are derived locally from approved context."""

    operation: FileOperation
    path: NonEmptyText
    content: SourceText
    rationale: NonEmptyText

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _safe_relative_posix(value)


class ProposedChangeSet(ContractModel):
    summary: NonEmptyText
    changes: list[ProposedFileChange] = Field(min_length=1)

    @model_validator(mode="after")
    def paths_are_unique(self) -> ProposedChangeSet:
        normalized = [change.path.casefold() for change in self.changes]
        if len(set(normalized)) != len(normalized):
            raise ValueError("a proposed change set cannot contain duplicate paths")
        return self


class ChangeSet(ContractModel):
    summary: NonEmptyText
    changes: list[FileChange] = Field(min_length=1)

    @model_validator(mode="after")
    def paths_are_unique(self) -> ChangeSet:
        normalized = [change.path.casefold() for change in self.changes]
        if len(set(normalized)) != len(normalized):
            raise ValueError("a change set cannot contain duplicate paths")
        return self


class TestFailure(ContractModel):
    test: NonEmptyText
    reason: NonEmptyText


class TestResult(ContractModel):
    command_id: NonEmptyText
    exit_code: int
    duration_ms: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    failure_summary: list[TestFailure] = Field(default_factory=list)
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> TestResult:
        if self.exit_code == 0 and self.failed:
            raise ValueError("a successful command cannot report failed tests")
        if self.exit_code != 0 and self.failed == 0 and not self.failure_summary:
            raise ValueError("a failed command must include a failed count or summary")
        return self

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and self.failed == 0


class RequirementCoverage(ContractModel):
    criterion: NonEmptyText
    status: CoverageStatus


class ReviewFinding(ContractModel):
    severity: FindingSeverity
    summary: NonEmptyText
    file: str | None = None

    @field_validator("file")
    @classmethod
    def file_is_safe_if_present(cls, value: str | None) -> str | None:
        return None if value is None else _safe_relative_posix(value)


class ReviewDecision(ContractModel):
    decision: ReviewOutcome
    requirement_coverage: list[RequirementCoverage]
    findings: list[ReviewFinding] = Field(default_factory=list)
    residual_risks: list[NonEmptyText] = Field(default_factory=list)
    summary: NonEmptyText


class RunSummary(ContractModel):
    run_id: NonEmptyText
    project_id: NonEmptyText
    requirement: NonEmptyText
    status: RunStatus
    current_node: str | None = None
    project_profile_hash: Sha256
    plan_hash: Sha256 | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    terminal_reason: str | None = None
    last_event_id: int | None = None
