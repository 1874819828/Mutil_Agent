"""Local-only HTTP routes for the MVD control plane."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.api.schemas import (
    ApprovalRequest,
    CancelRequest,
    CreateRunRequest,
    PublishRunRequest,
)
from app.api.security import bearer, require_local_control
from app.background import BackgroundRunWorker
from app.contracts import InvalidStatusTransition
from app.git_delivery import GitDeliveryError
from app.runtime import (
    AssistantRuntime,
    PublicationValidationError,
    RuntimeConfigurationError,
)
from app.storage import (
    ApprovalConflictError,
    ArtifactNotFoundError,
    IdempotencyConflictError,
    PublicationConflictError,
    RunNotFoundError,
)


def _local_control_dependency(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    require_local_control(request, credentials)


router = APIRouter(
    prefix="/api/v1", dependencies=[Depends(_local_control_dependency)]
)


def _runtime(request: Request) -> AssistantRuntime:
    return request.app.state.runtime


def _worker(request: Request) -> BackgroundRunWorker | None:
    return getattr(request.app.state, "worker", None)


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(payload: CreateRunRequest, request: Request) -> dict[str, Any]:
    try:
        run = _runtime(request).create_run(**payload.model_dump())
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    worker = _worker(request)
    if worker is not None:
        worker.wake()
    return _jsonable(run)


@router.get("/acceptance-packs")
def list_acceptance_packs(request: Request) -> list[dict[str, Any]]:
    return [_jsonable(pack) for pack in _runtime(request).list_acceptance_packs()]


@router.get("/runs")
def list_runs(
    request: Request, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    try:
        records = _runtime(request).store.list_runs(limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    items = []
    for record in records:
        data = _jsonable(record)
        data.pop("plan", None)  # 列表视图不返回完整计划,详情接口返回
        items.append(data)
    return items


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return _jsonable(_runtime(request).get_run(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/events")
def get_events(
    run_id: str, request: Request, after_event_id: int = 0
) -> list[dict[str, Any]]:
    try:
        _runtime(request).get_run(run_id)
        events = _runtime(request).store.list_events(
            run_id, after_event_id=after_event_id
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [_jsonable(event) for event in events]


@router.get("/runs/{run_id}/llm-calls")
def get_llm_calls(run_id: str, request: Request) -> list[dict[str, Any]]:
    try:
        calls = _runtime(request).store.list_llm_calls(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [_jsonable(call) for call in calls]


@router.post("/runs/{run_id}/approval")
def approve_run(
    run_id: str, payload: ApprovalRequest, request: Request
) -> dict[str, Any]:
    try:
        run = _runtime(request).approve_run(
            run_id,
            decision=payload.decision,
            plan_hash=payload.plan_hash,
            project_profile_hash=payload.project_profile_hash,
            source_manifest_hash=payload.source_manifest_hash,
            context_bundle_hash=payload.context_bundle_hash,
            acceptance_pack_hash=payload.acceptance_pack_hash,
            idempotency_key=payload.idempotency_key,
            reason=payload.comment,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        ApprovalConflictError,
        IdempotencyConflictError,
        InvalidStatusTransition,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    worker = _worker(request)
    if worker is not None:
        worker.wake()
    return _jsonable(run)


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_run(
    run_id: str, payload: CancelRequest, request: Request
) -> dict[str, Any]:
    try:
        run = _runtime(request).request_cancel(run_id, reason=payload.reason)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidStatusTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    worker = _worker(request)
    if worker is not None:
        worker.wake()
    return _jsonable(run)


@router.get("/runs/{run_id}/diff", response_class=PlainTextResponse)
def get_diff(run_id: str, request: Request) -> str:
    try:
        _runtime(request).get_run(run_id)
        return _runtime(request).get_diff(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail="diff is not available yet") from exc


@router.get("/runs/{run_id}/artifacts")
def list_artifacts(run_id: str, request: Request) -> list[dict[str, Any]]:
    try:
        return [
            _jsonable(item)
            for item in _runtime(request).store.list_artifacts(run_id)
        ]
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/publication")
def get_publication_preview(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return _jsonable(_runtime(request).publication_preview(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PublicationValidationError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/publication")
def publish_run(
    run_id: str, payload: PublishRunRequest, request: Request
) -> dict[str, Any]:
    try:
        result = _runtime(request).publish_run(
            run_id,
            confirmation=payload.confirmation,
            project_profile_hash=payload.project_profile_hash,
            source_manifest_hash=payload.source_manifest_hash,
            diff_sha256=payload.diff_sha256,
            git_original_branch=payload.git_original_branch,
            git_base_commit=payload.git_base_commit,
            git_target_branch=payload.git_target_branch,
            git_remote_name=payload.git_remote_name,
            git_remote_url=payload.git_remote_url,
            idempotency_key=payload.idempotency_key,
            comment=payload.comment,
        )
        return _jsonable(result)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        IdempotencyConflictError,
        GitDeliveryError,
        PublicationConflictError,
        PublicationValidationError,
        RuntimeConfigurationError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/artifacts/{artifact_id}")
def download_artifact(artifact_id: str, request: Request) -> Response:
    try:
        record, data = _runtime(request).read_artifact(artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="artifact file is unavailable") from exc
    filename = Path(record.relative_path).name.replace('"', "")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
