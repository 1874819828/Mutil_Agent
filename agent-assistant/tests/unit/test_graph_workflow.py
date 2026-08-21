from __future__ import annotations

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.contracts import ReviewOutcome, RunStatus
from app.graph import WorkflowNodes, build_workflow


def test_workflow_interrupts_for_approval_then_completes() -> None:
    nodes = _happy_nodes()
    graph = build_workflow(nodes, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "approval-flow"}}

    waiting = asyncio.run(graph.ainvoke(_initial_state(), config=config))

    assert waiting["status"] == RunStatus.WAITING_APPROVAL
    assert waiting["__interrupt__"]

    completed = asyncio.run(
        graph.ainvoke(
            Command(resume={"decision": "approve", "plan_hash": "plan-hash"}),
            config=config,
        )
    )

    assert completed["status"] == RunStatus.COMPLETED
    assert completed["developer_run_count"] == 1
    assert completed["test_fix_count"] == 0


def test_rejected_approval_never_calls_developer() -> None:
    calls = {"developer": 0}
    nodes = _happy_nodes(calls)
    graph = build_workflow(nodes, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "reject-flow"}}
    asyncio.run(graph.ainvoke(_initial_state(), config=config))

    rejected = asyncio.run(
        graph.ainvoke(Command(resume={"decision": "reject"}), config=config)
    )

    assert rejected["status"] == RunStatus.REJECTED
    assert calls["developer"] == 0


def test_stale_plan_approval_is_rejected_before_developer() -> None:
    calls = {"developer": 0}
    graph = build_workflow(_happy_nodes(calls), checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "stale-plan-flow"}}
    asyncio.run(graph.ainvoke(_initial_state(), config=config))

    try:
        asyncio.run(
            graph.ainvoke(
                Command(resume={"decision": "approve", "plan_hash": "stale"}),
                config=config,
            )
        )
    except ValueError as exc:
        assert "plan_hash" in str(exc)
    else:
        raise AssertionError("stale approval unexpectedly resumed the workflow")

    assert calls["developer"] == 0


def test_test_failures_retry_exactly_twice_then_need_human() -> None:
    calls = {"developer": 0}
    nodes = _happy_nodes(calls)

    async def always_fail(state):
        return {
            "baseline_test_result": {
                "command_id": "baseline_pytest",
                "exit_code": 1,
                "failed": 1,
                "failure_summary": [{"test": "test_x", "reason": "still failing"}],
            },
            "latest_test_result": {
                "command_id": "baseline_pytest",
                "exit_code": 1,
                "failed": 1,
                "failure_summary": [{"test": "test_x", "reason": "still failing"}],
            },
        }

    nodes = nodes._replace(baseline_tester=always_fail)
    graph = build_workflow(nodes, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "retry-flow"}}
    asyncio.run(graph.ainvoke(_initial_state(), config=config))

    exhausted = asyncio.run(
        graph.ainvoke(
            Command(resume={"decision": "approve", "plan_hash": "plan-hash"}),
            config=config,
        )
    )

    assert exhausted["status"] == RunStatus.NEEDS_HUMAN
    assert exhausted["test_fix_count"] == 2
    assert calls["developer"] == 3


def test_reviewer_fix_is_limited_to_one_and_retests() -> None:
    calls = {"developer": 0, "baseline": 0, "acceptance": 0, "reviewer": 0}
    nodes = _happy_nodes(calls)

    async def reviewer(state):
        calls["reviewer"] += 1
        return {"review": {"decision": ReviewOutcome.CHANGES_REQUESTED}}

    nodes = nodes._replace(reviewer=reviewer)
    graph = build_workflow(nodes, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "review-retry-flow"}}
    asyncio.run(graph.ainvoke(_initial_state(), config=config))

    exhausted = asyncio.run(
        graph.ainvoke(
            Command(resume={"decision": "approve", "plan_hash": "plan-hash"}),
            config=config,
        )
    )

    assert exhausted["status"] == RunStatus.NEEDS_HUMAN
    assert exhausted["review_fix_count"] == 1
    assert calls == {"developer": 2, "baseline": 2, "acceptance": 2, "reviewer": 2}


def _happy_nodes(calls: dict[str, int] | None = None) -> WorkflowNodes:
    calls = calls if calls is not None else {}

    async def manager(state):
        return {"plan": {"summary": "plan"}, "plan_hash": "plan-hash"}

    async def developer(state):
        calls["developer"] = calls.get("developer", 0) + 1
        return {
            "change_set": {"changes": []},
            "developer_run_count": state.get("developer_run_count", 0) + 1,
        }

    async def apply_change(state):
        return {"diff": "diff", "changed_files": ["backend/app/main.py"]}

    async def baseline(state):
        calls["baseline"] = calls.get("baseline", 0) + 1
        return {
            "baseline_test_result": {"exit_code": 0, "failed": 0},
            "latest_test_result": {"exit_code": 0, "failed": 0},
        }

    async def acceptance(state):
        calls["acceptance"] = calls.get("acceptance", 0) + 1
        return {
            "acceptance_test_result": {"exit_code": 0, "failed": 0},
            "latest_test_result": {"exit_code": 0, "failed": 0},
        }

    async def gate(state):
        return {"policy_passed": True, "policy_violations": []}

    async def reviewer(state):
        calls["reviewer"] = calls.get("reviewer", 0) + 1
        return {"review": {"decision": ReviewOutcome.APPROVE}}

    return WorkflowNodes(
        manager=manager,
        developer=developer,
        apply_change=apply_change,
        baseline_tester=baseline,
        acceptance_tester=acceptance,
        policy_gate=gate,
        reviewer=reviewer,
    )


def _initial_state() -> dict:
    return {
        "run_id": "run-1",
        "requirement": "add health endpoint",
        "status": RunStatus.RUNNING,
        "developer_run_count": 0,
        "test_run_count": 0,
        "test_fix_count": 0,
        "review_fix_count": 0,
        "metrics": {},
    }
