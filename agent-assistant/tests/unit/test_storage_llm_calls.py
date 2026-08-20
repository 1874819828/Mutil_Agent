from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.storage import LeaseLostError, SQLiteRunStore


def _claimed_store(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    run = store.create_run(
        requirement="generate a real structured plan",
        project_id="demo",
        project_profile_hash="a" * 64,
        source_manifest_hash="b" * 64,
        provider_mode="openai",
    )
    claim = store.claim_next_run("worker", lease_ttl=timedelta(seconds=30))
    assert claim is not None and claim.run.run_id == run.run_id
    return store, claim


def test_llm_call_budget_is_reserved_and_fenced(tmp_path: Path) -> None:
    store, claim = _claimed_store(tmp_path)
    reserved = store.reserve_llm_call(
        claim.token,
        call_id="call-1",
        role="manager",
        prompt_version="manager-plan-v1",
        request_hash="c" * 64,
        configured_model="configured-model",
        reserved_input_tokens=100,
        reserved_output_tokens=50,
        max_calls_per_run=2,
        max_input_tokens_per_run=200,
        max_output_tokens_per_run=100,
    )
    completed = store.finalize_llm_call(
        claim.token,
        call_id=reserved.call_id,
        status="succeeded",
        actual_model="resolved-model",
        response_id="resp-1",
        input_tokens=80,
        output_tokens=25,
    )

    assert completed.status == "succeeded"
    assert completed.actual_model == "resolved-model"
    assert store.list_llm_calls(claim.run.run_id) == [completed]

    store.request_cancel(claim.run.run_id)
    with pytest.raises(LeaseLostError):
        store.finalize_llm_call(
            claim.token,
            call_id="call-1",
            status="succeeded",
        )


def test_llm_call_reservation_refuses_run_budget_overflow(tmp_path: Path) -> None:
    store, claim = _claimed_store(tmp_path)
    kwargs = dict(
        role="developer",
        prompt_version="developer-v1",
        request_hash="d" * 64,
        configured_model="model",
        reserved_input_tokens=100,
        reserved_output_tokens=50,
        max_calls_per_run=1,
        max_input_tokens_per_run=100,
        max_output_tokens_per_run=50,
    )
    store.reserve_llm_call(claim.token, call_id="call-1", **kwargs)

    with pytest.raises(ValueError, match="call budget"):
        store.reserve_llm_call(claim.token, call_id="call-2", **kwargs)

