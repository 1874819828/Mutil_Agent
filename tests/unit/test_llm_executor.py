from __future__ import annotations

import asyncio
from datetime import timedelta

from app.contracts import TaskPlan
from app.llm import AgentRole, DeterministicMockProvider, FencedLLMCallExecutor
from app.storage import SQLiteRunStore


def test_executor_reserves_and_finalizes_call_metadata(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    run = store.create_run(
        project_id="demo",
        requirement="implement a safe change",
        project_profile_hash="a" * 64,
    )
    claimed = store.claim_next_run("worker", lease_ttl=timedelta(minutes=1))
    assert claimed is not None and claimed.run.run_id == run.run_id
    executor = FencedLLMCallExecutor(
        provider=DeterministicMockProvider(), store=store, token=claimed.token
    )

    generation = asyncio.run(
        executor.generate(
            role=AgentRole.MANAGER,
            response_model=TaskPlan,
            payload={
                "allowed_change_globs": ["backend/app/**/*.py"],
                "test_profile": "python-fastapi",
            },
            prompt_version="manager_plan/v1",
        )
    )

    assert isinstance(generation.output, TaskPlan)
    calls = store.list_llm_calls(run.run_id)
    assert len(calls) == 1
    assert calls[0].status == "succeeded"
    assert calls[0].role == "manager"
    assert calls[0].prompt_version == "manager_plan/v1"
    assert calls[0].configured_model == "deterministic-mock"


def test_executor_enforces_per_run_call_budget(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    run = store.create_run(
        project_id="demo",
        requirement="implement a safe change",
        project_profile_hash="b" * 64,
    )
    claimed = store.claim_next_run("worker", lease_ttl=timedelta(minutes=1))
    assert claimed is not None
    executor = FencedLLMCallExecutor(
        provider=DeterministicMockProvider(),
        store=store,
        token=claimed.token,
        max_calls_per_run=1,
    )
    payload = {
        "allowed_change_globs": ["backend/app/**/*.py"],
        "test_profile": "python-fastapi",
    }
    asyncio.run(
        executor.generate(
            role=AgentRole.MANAGER,
            response_model=TaskPlan,
            payload=payload,
            prompt_version="manager_plan/v1",
        )
    )

    try:
        asyncio.run(
            executor.generate(
                role=AgentRole.MANAGER,
                response_model=TaskPlan,
                payload=payload,
                prompt_version="manager_plan/v1",
            )
        )
    except ValueError as exc:
        assert "call budget exhausted" in str(exc)
    else:
        raise AssertionError("second call must not exceed the frozen budget")


def test_executor_reserves_full_prompt_and_schema_bytes(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    run = store.create_run(
        project_id="demo",
        requirement="实现严格的中文预算边界",
        project_profile_hash="c" * 64,
    )
    claimed = store.claim_next_run("worker", lease_ttl=timedelta(minutes=1))
    assert claimed is not None
    executor = FencedLLMCallExecutor(
        provider=DeterministicMockProvider(), store=store, token=claimed.token
    )
    asyncio.run(
        executor.generate(
            role=AgentRole.MANAGER,
            response_model=TaskPlan,
            payload={"requirement": "中文😀"},
            prompt_version="manager_plan/v1",
        )
    )
    call = store.list_llm_calls(run.run_id)[0]
    assert call.reserved_input_tokens > len("中文😀".encode("utf-8"))
