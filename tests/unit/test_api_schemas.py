from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import ApprovalRequest, CreateRunRequest


def test_create_run_strips_requirement() -> None:
    request = CreateRunRequest(project_id="student-management", requirement="  add a health endpoint  ")
    assert request.requirement == "add a health endpoint"


@pytest.mark.parametrize("project_id", ["../secret", "UPPER", "has space", ""])
def test_create_run_rejects_unsafe_project_ids(project_id: str) -> None:
    with pytest.raises(ValidationError):
        CreateRunRequest(project_id=project_id, requirement="a sufficiently clear requirement")


def test_approval_requires_hashes_and_idempotency_key() -> None:
    request = ApprovalRequest(
        decision="approve",
        plan_hash="a" * 64,
        project_profile_hash="b" * 64,
        idempotency_key="approval:run-123",
    )
    assert request.decision == "approve"

