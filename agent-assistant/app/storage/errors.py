"""Storage-specific failure types with API-mappable semantics."""

from __future__ import annotations


class StorageError(RuntimeError):
    pass


class RunNotFoundError(StorageError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run not found: {run_id}")


class ArtifactNotFoundError(StorageError):
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        super().__init__(f"artifact not found: {artifact_id}")


class ApprovalNotFoundError(StorageError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"approval not found for run: {run_id}")


class ApprovalConflictError(StorageError):
    pass


class PublicationConflictError(StorageError):
    pass


class IdempotencyConflictError(StorageError):
    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"idempotency key was already used for another request: {idempotency_key}")


class LeaseLostError(StorageError):
    """The supplied worker generation no longer owns an active run lease."""

    def __init__(self, run_id: str, worker_id: str, generation: int) -> None:
        self.run_id = run_id
        self.worker_id = worker_id
        self.generation = generation
        super().__init__(
            f"active lease lost for run={run_id}, worker={worker_id}, "
            f"generation={generation}"
        )
