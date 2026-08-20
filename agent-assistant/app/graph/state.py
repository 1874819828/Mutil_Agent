"""Compact LangGraph state for the MVD workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from app.contracts import ChangeSet, ReviewDecision, RunStatus, TaskPlan, TestResult
from app.sandbox import RunnerRequest


class RunState(TypedDict, total=False):
    run_id: str
    project_id: str
    project_profile_hash: str
    source_manifest_hash: str
    context_bundle_hash: str
    acceptance_pack_hash: str
    effective_change_globs: list[str]
    requirement: str
    status: RunStatus
    workspace_path: str
    repository_summary: dict[str, Any]
    repository_files: dict[str, str]
    test_profile: str
    plan: TaskPlan | dict[str, Any]
    plan_hash: str
    approval: dict[str, Any]
    approval_prevalidated: bool
    change_set: ChangeSet | dict[str, Any]
    runner_requests: dict[str, RunnerRequest]
    baseline_test_result: TestResult | dict[str, Any]
    acceptance_test_result: TestResult | dict[str, Any]
    developer_test_result: TestResult | dict[str, Any]
    latest_test_result: TestResult | dict[str, Any]
    review: ReviewDecision | dict[str, Any]
    developer_run_count: int
    test_run_count: int
    test_fix_count: int
    review_fix_count: int
    last_event_id: int | None
    metrics: dict[str, Any]
    diff: str
    changed_files: list[str]
    scope_violations: list[str]
    baseline_modified: bool
    secret_findings: list[str]
    policy_passed: bool
    policy_violations: list[str]
    final_result: dict[str, Any]
