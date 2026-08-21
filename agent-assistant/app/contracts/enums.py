"""Shared enum contracts for workflow inputs, outputs, and persisted state."""

from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    """A JSON-friendly enum with stable string values."""

    def __str__(self) -> str:
        return self.value


class RunStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    REJECTED = "rejected"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FileOperation(StringEnum):
    CREATE = "create"
    UPDATE = "update"


class ReviewOutcome(StringEnum):
    APPROVE = "approve"
    CHANGES_REQUESTED = "changes_requested"


class ApprovalOutcome(StringEnum):
    APPROVE = "approve"
    REJECT = "reject"


class CoverageStatus(StringEnum):
    MET = "met"
    PARTIAL = "partial"
    UNMET = "unmet"


class FindingSeverity(StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.REJECTED,
        RunStatus.NEEDS_HUMAN,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)
