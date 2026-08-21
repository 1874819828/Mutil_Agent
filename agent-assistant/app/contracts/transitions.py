"""The closed, explicit run status transition model from the MVP blueprint."""

from __future__ import annotations

from .enums import RunStatus


class InvalidStatusTransition(ValueError):
    """Raised when code attempts a transition outside the frozen state model."""

    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"run status cannot transition from {current.value} to {target.value}")


ALLOWED_STATUS_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.COMPLETED,
            RunStatus.NEEDS_HUMAN,
            RunStatus.FAILED,
            RunStatus.QUEUED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.QUEUED, RunStatus.CANCEL_REQUESTED, RunStatus.REJECTED}
    ),
    RunStatus.CANCEL_REQUESTED: frozenset({RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.REJECTED: frozenset(),
    RunStatus.NEEDS_HUMAN: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in ALLOWED_STATUS_TRANSITIONS[current]


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    if not can_transition(current, target):
        raise InvalidStatusTransition(current, target)

