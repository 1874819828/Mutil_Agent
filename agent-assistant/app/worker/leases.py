"""Small worker-facing facade over the store's lease primitives."""

from __future__ import annotations

from datetime import timedelta

from app.contracts import RunStatus
from app.storage import ClaimedRun, LeaseToken, RunRecord, SQLiteRunStore


class LeaseCoordinator:
    """Binds a store to one worker identity and lease duration.

    It deliberately contains no polling loop. Application lifecycle code can
    schedule ``claim`` and ``renew`` without sharing mutable lease state here.
    """

    def __init__(
        self,
        store: SQLiteRunStore,
        *,
        worker_id: str,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl must be positive")
        self.store = store
        self.worker_id = worker_id.strip()
        self.lease_ttl = lease_ttl

    def claim(self) -> ClaimedRun | None:
        return self.store.claim_next_run(self.worker_id, lease_ttl=self.lease_ttl)

    def renew(self, token: LeaseToken) -> LeaseToken:
        self._require_own_token(token)
        return self.store.renew_lease(token, lease_ttl=self.lease_ttl)

    def release_for_retry(self, token: LeaseToken, *, reason: str) -> RunRecord:
        self._require_own_token(token)
        return self.store.transition_fenced(token, RunStatus.QUEUED, reason=reason)

    def _require_own_token(self, token: LeaseToken) -> None:
        if token.worker_id != self.worker_id:
            raise ValueError("lease token belongs to a different worker")

