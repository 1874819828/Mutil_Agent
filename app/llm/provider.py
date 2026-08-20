"""Vendor-neutral structured LLM provider contract and deterministic mock."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import secrets
from typing import Any, Generic, Protocol, TypeVar, cast

from pydantic import BaseModel

from app.contracts import (
    ChangeSet,
    CoverageStatus,
    FileChange,
    FileOperation,
    FindingSeverity,
    RequirementCoverage,
    ReviewDecision,
    ReviewFinding,
    ReviewOutcome,
    ProposedChangeSet,
    RiskLevel,
    TaskPlan,
    TaskStep,
)


StructuredT = TypeVar("StructuredT", bound=BaseModel)
MockResponse = BaseModel | Mapping[str, Any] | Callable[[Mapping[str, Any]], BaseModel | Mapping[str, Any]]
CancelCheck = Callable[[], bool]


class AgentRole(str, Enum):
    MANAGER = "manager"
    DEVELOPER = "developer"
    TESTER = "tester"
    REVIEWER = "reviewer"


@dataclass(frozen=True, slots=True)
class LLMCallMetadata:
    provider: str
    configured_model: str
    actual_model: str | None
    response_id: str | None
    prompt_version: str
    prompt_sha256: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    attempts: int
    latency_ms: int
    billing_unknown: bool = False


@dataclass(frozen=True, slots=True)
class LLMGeneration(Generic[StructuredT]):
    output: StructuredT
    metadata: LLMCallMetadata


@dataclass(frozen=True, slots=True)
class LLMCallContext:
    """Runtime-issued authority for one model call.

    The private seal prevents values copied from an API request from becoming a
    valid call authority.  Only ``LLMCallAuthority.issue`` can create a context
    accepted by the provider.  The context is immutable and carries no secret.
    """

    run_id: str
    generation: int
    call_id: str
    prompt_version: str
    budget_reservation_id: str
    reserved_output_tokens: int
    _is_cancelled: CancelCheck = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def assert_authorized(self) -> None:
        if self._seal is not _CALL_CONTEXT_SEAL:
            raise LLMUnauthorizedContextError("LLM call context was not issued by the runtime authority")

    @property
    def cancelled(self) -> bool:
        self.assert_authorized()
        return bool(self._is_cancelled())


_CALL_CONTEXT_SEAL = object()


class LLMCallAuthority:
    """Factory used by the trusted runtime, never by HTTP request parsing."""

    def issue(
        self,
        *,
        run_id: str,
        generation: int,
        call_id: str,
        prompt_version: str,
        budget_reservation_id: str,
        reserved_output_tokens: int,
        is_cancelled: CancelCheck | None = None,
    ) -> LLMCallContext:
        values = (run_id, call_id, prompt_version, budget_reservation_id)
        if any(not value.strip() for value in values):
            raise ValueError("LLM call authority fields must be non-empty")
        if generation < 1:
            raise ValueError("generation must be positive")
        if reserved_output_tokens < 1:
            raise ValueError("reserved_output_tokens must be positive")
        return LLMCallContext(
            run_id=run_id,
            generation=generation,
            call_id=call_id,
            prompt_version=prompt_version,
            budget_reservation_id=budget_reservation_id,
            reserved_output_tokens=reserved_output_tokens,
            _is_cancelled=is_cancelled or (lambda: False),
            _seal=_CALL_CONTEXT_SEAL,
        )


@dataclass(frozen=True, slots=True)
class ProviderCall:
    context: LLMCallContext
    role: AgentRole
    response_model: type[BaseModel]
    payload: Mapping[str, Any]


class LLMProvider(Protocol):
    """The only model-facing dependency used by logical role nodes."""

    async def generate(
        self,
        *,
        context: LLMCallContext,
        role: AgentRole,
        response_model: type[StructuredT],
        payload: Mapping[str, Any],
    ) -> LLMGeneration[StructuredT]:
        """Return validated output plus non-sensitive provider metadata."""


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        billing_unknown: bool = False,
        response_id: str | None = None,
        request_id: str | None = None,
        status_code: int | None = None,
        provider_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.billing_unknown = billing_unknown
        self.response_id = response_id
        self.request_id = request_id
        self.status_code = status_code
        self.provider_code = provider_code


class LLMUnauthorizedContextError(LLMProviderError):
    pass


class LLMCancelledError(LLMProviderError):
    pass


class LLMRateLimitError(LLMProviderError):
    pass


class LLMTimeoutError(LLMProviderError):
    pass


class LLMAuthenticationError(LLMProviderError):
    pass


class LLMRefusalError(LLMProviderError):
    pass


class LLMStructuredOutputError(LLMProviderError):
    pass


class MockProviderError(LLMProviderError):
    """A deterministic fixture is missing or incompatible."""


class DeterministicMockProvider:
    """Repeatable offline provider used for tests and the original MVD path."""

    def __init__(
        self,
        responses: Mapping[AgentRole | str, Sequence[MockResponse]] | None = None,
    ) -> None:
        self._responses: dict[AgentRole, deque[MockResponse]] = defaultdict(deque)
        for role, fixtures in (responses or {}).items():
            self._responses[AgentRole(role)].extend(fixtures)
        self.calls: list[ProviderCall] = []

    async def generate(
        self,
        *,
        context: LLMCallContext,
        role: AgentRole,
        response_model: type[StructuredT],
        payload: Mapping[str, Any],
    ) -> LLMGeneration[StructuredT]:
        context.assert_authorized()
        if context.cancelled:
            raise LLMCancelledError("LLM call was cancelled before mock generation")
        frozen_payload = _copy_payload(payload)
        self.calls.append(
            ProviderCall(
                context=context,
                role=role,
                response_model=cast(type[BaseModel], response_model),
                payload=frozen_payload,
            )
        )
        queue = self._responses[role]
        if queue:
            fixture = queue.popleft()
            value = fixture(frozen_payload) if callable(fixture) else fixture
        else:
            value = self._default(role, response_model, frozen_payload)
        if response_model is ProposedChangeSet and isinstance(value, ChangeSet):
            value = {
                "summary": value.summary,
                "changes": [
                    {
                        "operation": item.operation.value,
                        "path": item.path,
                        "content": item.content,
                        "rationale": item.rationale,
                    }
                    for item in value.changes
                ],
            }
        try:
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="python")
            output = response_model.model_validate(value)
        except Exception as exc:
            raise MockProviderError(
                f"mock response for role {role.value!r} does not satisfy {response_model.__name__}"
            ) from exc
        return LLMGeneration(
            output=output,
            metadata=LLMCallMetadata(
                provider="mock",
                configured_model="deterministic-mock",
                actual_model="deterministic-mock",
                response_id=f"mock-{secrets.token_hex(8)}",
                prompt_version=context.prompt_version,
                prompt_sha256=hashlib.sha256(context.prompt_version.encode()).hexdigest(),
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                attempts=1,
                latency_ms=0,
            ),
        )

    def _default(
        self,
        role: AgentRole,
        response_model: type[BaseModel],
        payload: Mapping[str, Any],
    ) -> BaseModel | Mapping[str, Any]:
        if role is AgentRole.MANAGER and response_model is not TaskPlan:
            # InspectionRequest is owned by the contracts package and may be
            # introduced independently.  Shape-based construction keeps this
            # provider decoupled while still supporting the two-stage Manager.
            return {
                "files": [
                    {"path": "backend/app/main.py", "reason": "inspect application routes"},
                    {"path": "backend/app/database.py", "reason": "inspect database integration"},
                ]
            }
        if role is AgentRole.MANAGER:
            globs = list(payload.get("allowed_change_globs") or ["backend/app/**/*.py"])
            test_profile = str(payload.get("test_profile") or "python-fastapi")
            return TaskPlan(
                summary="Add the student-management health endpoint",
                acceptance_criteria=[
                    "healthy database returns HTTP 200 with ok status",
                    "database failure returns HTTP 503 without leaking internals",
                    "existing routes remain available",
                ],
                files_to_inspect=["backend/app/main.py", "backend/app/database.py"],
                allowed_change_globs=globs,
                steps=[
                    TaskStep(
                        id="T1",
                        description="Implement a database-aware health endpoint",
                        depends_on=[],
                    )
                ],
                risk_level=RiskLevel.LOW,
                test_profile=test_profile,
            )
        if role is AgentRole.DEVELOPER:
            fixture = _health_change_set(payload)
            if response_model is ProposedChangeSet:
                return {
                    "summary": fixture.summary,
                    "changes": [
                        {
                            "operation": item.operation.value,
                            "path": item.path,
                            "content": item.content,
                            "rationale": item.rationale,
                        }
                        for item in fixture.changes
                    ],
                }
            return fixture
        if role is AgentRole.REVIEWER:
            return _review(payload)
        raise MockProviderError(
            "Tester commands and outcomes come from TestRunner; no model fixture was supplied"
        )


def _health_change_set(payload: Mapping[str, Any]) -> ChangeSet:
    files = payload.get("repository_files")
    if not isinstance(files, Mapping):
        raise MockProviderError("default Developer fixture requires repository_files")
    path = "backend/app/main.py"
    current = files.get(path)
    if not isinstance(current, str):
        raise MockProviderError(f"default Developer fixture requires {path}")
    updated = current
    if 'from fastapi.responses import JSONResponse' not in updated:
        updated = updated.replace(
            "from fastapi import FastAPI\n",
            "from fastapi import FastAPI\nfrom fastapi.responses import JSONResponse\nfrom sqlalchemy import text\n",
            1,
        )
    if '"/api/v1/health"' not in updated and "'/api/v1/health'" not in updated:
        route = '''\n\n@app.get("/api/v1/health")
async def health():
    try:
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": "error"},
        )
'''
        marker = '\n\nif __name__ == "__main__":'
        updated = updated.replace(marker, f"{route}{marker}", 1)
    return ChangeSet(
        summary="Implement the health endpoint in the copied workspace",
        changes=[
            FileChange(
                operation=FileOperation.UPDATE,
                path=path,
                base_sha256=hashlib.sha256(current.encode("utf-8")).hexdigest(),
                content=updated,
                rationale="Provide deterministic database health behavior for the golden task",
            )
        ],
    )


def _review(payload: Mapping[str, Any]) -> ReviewDecision:
    plan = payload.get("plan")
    criteria = plan.get("acceptance_criteria", []) if isinstance(plan, Mapping) else []
    tests = (payload.get("baseline_test_result"), payload.get("acceptance_test_result"))
    tests_passed = all(
        isinstance(result, Mapping)
        and result.get("exit_code") == 0
        and result.get("failed", 0) == 0
        for result in tests
    )
    decision = ReviewOutcome.APPROVE if tests_passed else ReviewOutcome.CHANGES_REQUESTED
    findings = []
    if not tests_passed:
        findings.append(
            ReviewFinding(
                severity=FindingSeverity.HIGH,
                summary="Immutable baseline or acceptance tests did not pass",
            )
        )
    return ReviewDecision(
        decision=decision,
        requirement_coverage=[
            RequirementCoverage(
                criterion=str(criterion),
                status=CoverageStatus.MET if tests_passed else CoverageStatus.UNMET,
            )
            for criterion in criteria
        ],
        findings=findings,
        residual_risks=[],
        summary=(
            "All immutable tests passed and the change covers the approved plan"
            if tests_passed
            else "The change cannot be approved while immutable tests fail"
        ),
    )


def _copy_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, BaseModel):
            copied[key] = value.model_dump(mode="json")
        elif isinstance(value, Mapping):
            copied[key] = _copy_payload(value)
        elif isinstance(value, list):
            copied[key] = [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in value
            ]
        else:
            copied[key] = value
    return copied
