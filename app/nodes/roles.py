"""Pure, structured implementations of the four logical role nodes."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from app.contracts import (
    ChangeSet,
    InspectionRequest,
    FileChange,
    FileOperation,
    ProposedChangeSet,
    ReviewDecision,
    TaskPlan,
    TestFailure,
    TestResult,
    canonical_json_hash,
)
from app.llm import AgentRole, LLMCallAuthority, LLMGeneration, LLMProvider
from app.sandbox import ExecutionResult, RunnerRequest, TestRunner


State = Mapping[str, Any]
StructuredT = TypeVar("StructuredT", bound=BaseModel)


class NodeLLMExecutor(Protocol):
    async def generate(
        self,
        *,
        role: AgentRole,
        response_model: type[StructuredT],
        payload: Mapping[str, Any],
        prompt_version: str,
    ) -> LLMGeneration[StructuredT]: ...


class _StandaloneExecutor:
    """Compatibility adapter for isolated node tests and library callers."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self._authority = LLMCallAuthority()

    async def generate(
        self,
        *,
        role: AgentRole,
        response_model: type[StructuredT],
        payload: Mapping[str, Any],
        prompt_version: str,
    ) -> LLMGeneration[StructuredT]:
        call_id = str(uuid4())
        context = self._authority.issue(
            run_id="standalone-node",
            generation=1,
            call_id=call_id,
            prompt_version=prompt_version,
            budget_reservation_id=call_id,
            reserved_output_tokens=20_000,
        )
        return await self._provider.generate(
            context=context,
            role=role,
            response_model=response_model,
            payload=payload,
        )


class ArtifactSink(Protocol):
    """Optional persistence port; implemented outside the node package."""

    def write_text(self, *, run_id: str, name: str, content: str) -> str: ...


class ManagerNode:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        executor: NodeLLMExecutor | None = None,
    ) -> None:
        self._executor = executor or _StandaloneExecutor(provider)

    async def __call__(self, state: State) -> dict[str, Any]:
        payload = {
            "requirement": state["requirement"],
            "repository_summary": state.get("repository_summary", {}),
            # This must be the already-frozen project scope.  The workspace
            # policy later combines it with the Manager's requested globs.
            "allowed_change_globs": list(state.get("effective_change_globs", [])),
            "test_profile": state.get("test_profile", "python-fastapi"),
        }
        generation = await self._executor.generate(
            role=AgentRole.MANAGER,
            response_model=TaskPlan,
            payload=payload,
            prompt_version="manager_plan/v1",
        )
        plan = generation.output
        return {
            "plan": plan,
            "plan_hash": canonical_json_hash(plan),
        }


class ManagerContextNode:
    """First Manager stage: select exact repository paths, without content."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        executor: NodeLLMExecutor | None = None,
    ) -> None:
        self._executor = executor or _StandaloneExecutor(provider)

    async def __call__(self, payload: Mapping[str, Any]) -> InspectionRequest:
        generation = await self._executor.generate(
            role=AgentRole.MANAGER,
            response_model=InspectionRequest,
            payload=payload,
            prompt_version="manager_select_context/v1",
        )
        return generation.output


class DeveloperNode:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        executor: NodeLLMExecutor | None = None,
    ) -> None:
        self._executor = executor or _StandaloneExecutor(provider)

    async def __call__(self, state: State) -> dict[str, Any]:
        payload = {
            "plan": _dump(state["plan"]),
            "repository_files": dict(state.get("repository_files", {})),
            # Full stdout/stderr artifacts and hidden assertions never enter
            # Developer context.  Only the explicit safe summaries do.
            "test_feedback": _safe_test_feedback(state.get("latest_test_result")),
            "review_feedback": _safe_review_feedback(state.get("review")),
        }
        generation = await self._executor.generate(
            role=AgentRole.DEVELOPER,
            response_model=ProposedChangeSet,
            payload=payload,
            prompt_version="developer/v1",
        )
        proposal = generation.output
        repository_files = payload["repository_files"]
        changes: list[FileChange] = []
        for item in proposal.changes:
            current = repository_files.get(item.path)
            if item.operation is FileOperation.UPDATE:
                if not isinstance(current, str):
                    raise ValueError(
                        f"Developer proposed updating an unapproved or absent file: {item.path}"
                    )
                base_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
            else:
                if current is not None:
                    raise ValueError(
                        f"Developer proposed creating an existing file: {item.path}"
                    )
                base_sha256 = None
            changes.append(
                FileChange(
                    operation=item.operation,
                    path=item.path,
                    base_sha256=base_sha256,
                    content=item.content,
                    rationale=item.rationale,
                )
            )
        change_set = ChangeSet(summary=proposal.summary, changes=changes)
        return {
            "change_set": change_set,
            "developer_run_count": int(state.get("developer_run_count", 0)) + 1,
        }


class TesterNode:
    """Deterministic Tester: command selection is constructor-owned."""

    def __init__(
        self,
        runner: TestRunner,
        command_id: str,
        *,
        result_key: str | None = None,
        artifact_sink: ArtifactSink | None = None,
    ) -> None:
        self._runner = runner
        self._command_id = command_id
        self._result_key = result_key or _result_key(command_id)
        self._artifact_sink = artifact_sink

    def __call__(self, state: State) -> dict[str, Any]:
        requests = state.get("runner_requests", {})
        request = requests.get(self._command_id) if isinstance(requests, Mapping) else None
        if not isinstance(request, RunnerRequest):
            raise ValueError(f"runner request {self._command_id!r} is missing")
        execution = self._runner.run(request)
        stdout_artifact = self._write_artifact(state, "stdout", execution.stdout)
        stderr_artifact = self._write_artifact(state, "stderr", execution.stderr)
        failures = [TestFailure(test=test, reason=reason) for test, reason in execution.failures]
        if execution.exit_code != 0 and execution.failed == 0 and not failures:
            failures = [TestFailure(test="runner_command", reason="test command failed")]
        result = TestResult(
            command_id=self._command_id,
            exit_code=execution.exit_code,
            duration_ms=execution.duration_ms,
            passed=execution.passed,
            failed=execution.failed,
            failure_summary=failures,
            stdout_artifact=stdout_artifact,
            stderr_artifact=stderr_artifact,
        )
        return {
            self._result_key: result,
            "latest_test_result": result,
            "test_run_count": int(state.get("test_run_count", 0)) + 1,
        }

    def _write_artifact(self, state: State, stream: str, content: str) -> str | None:
        if self._artifact_sink is None or not content:
            return None
        return self._artifact_sink.write_text(
            run_id=str(state["run_id"]),
            name=f"{self._command_id}.{stream}.txt",
            content=content,
        )


class ReviewerNode:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        executor: NodeLLMExecutor | None = None,
    ) -> None:
        self._executor = executor or _StandaloneExecutor(provider)

    async def __call__(self, state: State) -> dict[str, Any]:
        payload = {
            "requirement": state["requirement"],
            "plan": _dump(state["plan"]),
            "diff": state.get("diff", ""),
            "changed_files": list(state.get("changed_files", [])),
            "baseline_test_result": _dump(state.get("baseline_test_result")),
            "acceptance_test_result": _dump(state.get("acceptance_test_result")),
        }
        generation = await self._executor.generate(
            role=AgentRole.REVIEWER,
            response_model=ReviewDecision,
            payload=payload,
            prompt_version="reviewer/v1",
        )
        review = generation.output
        return {"review": review}


class ExtraPolicyChecks(Protocol):
    def evaluate(self, state: State) -> list[str]: ...


class PolicyGateNode:
    """Non-overridable deterministic checks before semantic review."""

    def __init__(self, checker: ExtraPolicyChecks | None = None, *, max_patch_bytes: int = 200_000):
        self._checker = checker
        self._max_patch_bytes = max_patch_bytes

    def __call__(self, state: State) -> dict[str, Any]:
        violations: list[str] = []
        for name in ("baseline_test_result", "acceptance_test_result"):
            result = state.get(name)
            if not _test_succeeded(result):
                violations.append(f"{name} did not pass")
        violations.extend(str(item) for item in state.get("scope_violations", []))
        if state.get("baseline_modified"):
            violations.append("immutable baseline tests were modified")
        if state.get("secret_findings"):
            violations.append("sensitive content was detected in the patch")
        diff = str(state.get("diff", ""))
        if len(diff.encode("utf-8")) > self._max_patch_bytes:
            violations.append("patch exceeds the deterministic size limit")
        if self._checker is not None:
            violations.extend(self._checker.evaluate(state))
        unique = list(dict.fromkeys(violations))
        return {"policy_passed": not unique, "policy_violations": unique}


def _safe_test_feedback(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    dumped = _dump(value)
    if not isinstance(dumped, Mapping):
        return []
    summary = dumped.get("failure_summary", [])
    if not isinstance(summary, list):
        return []
    return [
        {"test": str(item.get("test", "test")), "reason": str(item.get("reason", "failed"))}
        for item in summary
        if isinstance(item, Mapping)
    ]


def _safe_review_feedback(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    dumped = _dump(value)
    if not isinstance(dumped, Mapping):
        return None
    return {
        "decision": dumped.get("decision"),
        "summary": dumped.get("summary"),
        "findings": dumped.get("findings", []),
    }


def _test_succeeded(value: Any) -> bool:
    if isinstance(value, TestResult):
        return value.succeeded
    if isinstance(value, Mapping):
        return value.get("exit_code") == 0 and value.get("failed", 0) == 0
    return False


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _result_key(command_id: str) -> str:
    if "baseline" in command_id:
        return "baseline_test_result"
    if "acceptance" in command_id:
        return "acceptance_test_result"
    return "developer_test_result"
