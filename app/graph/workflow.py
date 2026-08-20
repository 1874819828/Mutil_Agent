"""LangGraph topology for the bounded four-role feedback loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NamedTuple

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.contracts import ReviewOutcome, RunStatus

from .state import RunState


NodeCallable = Callable[[RunState], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class WorkflowNodes(NamedTuple):
    """All side-effecting behavior is injected through these node ports."""

    manager: NodeCallable
    developer: NodeCallable
    apply_change: NodeCallable
    baseline_tester: NodeCallable
    acceptance_tester: NodeCallable
    policy_gate: NodeCallable
    reviewer: NodeCallable


def build_workflow(nodes: WorkflowNodes, *, checkpointer: Any = None):
    """Compile the bounded MVD graph.

    The checkpointer is deliberately injected so storage remains outside this
    package.  Production callers should supply a durable SQLite checkpointer;
    tests can use LangGraph's in-memory saver.
    """

    graph = StateGraph(RunState)
    graph.add_node("dispatch", _dispatch)
    graph.add_node("manager", nodes.manager)
    graph.add_node("prepare_approval", _prepare_approval)
    graph.add_node("wait_for_plan_approval", _wait_for_plan_approval)
    graph.add_node("developer", nodes.developer)
    graph.add_node("apply_change", nodes.apply_change)
    graph.add_node("baseline_tester", nodes.baseline_tester)
    graph.add_node("acceptance_tester", nodes.acceptance_tester)
    graph.add_node("test_retry", _test_retry)
    graph.add_node("policy_gate", nodes.policy_gate)
    graph.add_node("reviewer", nodes.reviewer)
    graph.add_node("review_retry", _review_retry)
    graph.add_node("completed", _completed)
    graph.add_node("rejected", _rejected)
    graph.add_node("needs_human", _needs_human)

    graph.add_edge(START, "dispatch")
    graph.add_conditional_edges(
        "dispatch",
        _dispatch_route,
        {"new": "manager", "approved": "developer"},
    )
    graph.add_edge("manager", "prepare_approval")
    graph.add_edge("prepare_approval", "wait_for_plan_approval")
    graph.add_conditional_edges(
        "wait_for_plan_approval",
        _approval_route,
        {"approved": "developer", "rejected": "rejected"},
    )
    graph.add_edge("developer", "apply_change")
    graph.add_edge("apply_change", "baseline_tester")
    graph.add_conditional_edges(
        "baseline_tester",
        lambda state: _test_route(state, "baseline_test_result"),
        {"passed": "acceptance_tester", "retry": "test_retry", "exhausted": "needs_human"},
    )
    graph.add_conditional_edges(
        "acceptance_tester",
        lambda state: _test_route(state, "acceptance_test_result"),
        {"passed": "policy_gate", "retry": "test_retry", "exhausted": "needs_human"},
    )
    graph.add_edge("test_retry", "developer")
    graph.add_conditional_edges(
        "policy_gate",
        _policy_route,
        {"passed": "reviewer", "failed": "needs_human"},
    )
    graph.add_conditional_edges(
        "reviewer",
        _review_route,
        {"approved": "completed", "retry": "review_retry", "exhausted": "needs_human"},
    )
    graph.add_edge("review_retry", "developer")
    graph.add_edge("completed", END)
    graph.add_edge("rejected", END)
    graph.add_edge("needs_human", END)
    return graph.compile(checkpointer=checkpointer)


def _dispatch(state: RunState) -> dict[str, Any]:
    return {}


def _dispatch_route(state: RunState) -> str:
    # Runtime recovery starts a fresh, generation-fenced graph from the plan
    # durably stored in SQLite.  Standalone graph users still exercise the
    # native interrupt/resume path below.
    return "approved" if state.get("approval_prevalidated") is True else "new"


def _prepare_approval(state: RunState) -> dict[str, Any]:
    return {"status": RunStatus.WAITING_APPROVAL}


def _wait_for_plan_approval(state: RunState) -> dict[str, Any]:
    approval = interrupt(
        {
            "run_id": state["run_id"],
            "plan": _dump(state.get("plan")),
            "plan_hash": state.get("plan_hash"),
            "project_profile_hash": state.get("project_profile_hash"),
            "source_manifest_hash": state.get("source_manifest_hash"),
            "context_bundle_hash": state.get("context_bundle_hash"),
            "acceptance_pack_hash": state.get("acceptance_pack_hash"),
        }
    )
    if not isinstance(approval, Mapping):
        raise ValueError("approval resume payload must be an object")
    decision = str(approval.get("decision", "")).lower()
    if decision not in {"approve", "reject"}:
        raise ValueError("approval decision must be 'approve' or 'reject'")
    if decision == "approve":
        expected_plan_hash = state.get("plan_hash")
        if approval.get("plan_hash") != expected_plan_hash:
            raise ValueError("approval is stale: plan_hash does not match the paused plan")
        expected_profile_hash = state.get("project_profile_hash")
        if expected_profile_hash is not None and approval.get("project_profile_hash") != expected_profile_hash:
            raise ValueError("approval is stale: project_profile_hash does not match the frozen profile")
        for name in (
            "source_manifest_hash",
            "context_bundle_hash",
            "acceptance_pack_hash",
        ):
            expected = state.get(name)
            if expected is not None and approval.get(name) != expected:
                raise ValueError(f"approval is stale: {name} does not match the frozen run")
    return {
        "approval": dict(approval),
        "status": RunStatus.RUNNING if decision == "approve" else RunStatus.REJECTED,
    }


def _approval_route(state: RunState) -> str:
    approval = state.get("approval", {})
    return "approved" if approval.get("decision") == "approve" else "rejected"


def _test_route(state: RunState, result_key: str) -> str:
    if _test_succeeded(state.get(result_key)):
        return "passed"
    return "retry" if int(state.get("test_fix_count", 0)) < 2 else "exhausted"


def _test_retry(state: RunState) -> dict[str, Any]:
    return {"test_fix_count": int(state.get("test_fix_count", 0)) + 1}


def _policy_route(state: RunState) -> str:
    return "passed" if state.get("policy_passed") is True else "failed"


def _review_route(state: RunState) -> str:
    review = _dump(state.get("review"))
    decision = review.get("decision") if isinstance(review, Mapping) else None
    if decision == ReviewOutcome.APPROVE or decision == ReviewOutcome.APPROVE.value:
        return "approved"
    return "retry" if int(state.get("review_fix_count", 0)) < 1 else "exhausted"


def _review_retry(state: RunState) -> dict[str, Any]:
    return {"review_fix_count": int(state.get("review_fix_count", 0)) + 1}


def _completed(state: RunState) -> dict[str, Any]:
    return {
        "status": RunStatus.COMPLETED,
        "final_result": {
            "outcome": RunStatus.COMPLETED.value,
            "diff": state.get("diff", ""),
            "review": _dump(state.get("review")),
            "test_fix_count": int(state.get("test_fix_count", 0)),
            "review_fix_count": int(state.get("review_fix_count", 0)),
        },
    }


def _rejected(state: RunState) -> dict[str, Any]:
    return {
        "status": RunStatus.REJECTED,
        "final_result": {"outcome": RunStatus.REJECTED.value, "reason": "plan rejected by user"},
    }


def _needs_human(state: RunState) -> dict[str, Any]:
    reason = "bounded repair budget exhausted"
    if state.get("policy_violations"):
        reason = "deterministic policy gate rejected the change"
    return {
        "status": RunStatus.NEEDS_HUMAN,
        "final_result": {
            "outcome": RunStatus.NEEDS_HUMAN.value,
            "reason": reason,
            "policy_violations": list(state.get("policy_violations", [])),
            "test_fix_count": int(state.get("test_fix_count", 0)),
            "review_fix_count": int(state.get("review_fix_count", 0)),
        },
    }


def _test_succeeded(value: Any) -> bool:
    if hasattr(value, "succeeded"):
        return bool(value.succeeded)
    if isinstance(value, Mapping):
        return value.get("exit_code") == 0 and value.get("failed", 0) == 0
    return False


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value
