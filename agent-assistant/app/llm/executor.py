"""Generation-fenced LLM call budgeting and metadata persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from app.config.llm_settings import LLMSettings, ProviderMode
from app.contracts import canonical_json_hash
from app.storage import LeaseToken, SQLiteRunStore

from .provider import (
    AgentRole,
    LLMCallAuthority,
    LLMGeneration,
    LLMProvider,
    LLMProviderError,
)
from .prompts import PromptRegistry


StructuredT = TypeVar("StructuredT", bound=BaseModel)


_OUTPUT_RESERVATIONS = {
    "manager_select_context/v1": 2_000,
    "manager_plan/v1": 8_000,
    "developer/v1": 20_000,
    "reviewer/v1": 6_000,
}
_PROVIDER_REQUEST_OVERHEAD_BYTES = 4_096


class FencedLLMCallExecutor:
    """Reserve budget, invoke a provider, then publish metadata under one lease."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        store: SQLiteRunStore,
        token: LeaseToken,
        settings: LLMSettings | None = None,
        max_calls_per_run: int = 9,
        max_input_tokens_per_run: int = 200_000,
        max_output_tokens_per_run: int = 110_000,
    ) -> None:
        self._provider = provider
        self._store = store
        self._token = token
        self._settings = settings
        self._max_calls = max_calls_per_run
        self._max_input = max_input_tokens_per_run
        self._max_output = max_output_tokens_per_run
        self._authority = LLMCallAuthority()
        self._prompts = PromptRegistry()

    async def generate(
        self,
        *,
        role: AgentRole,
        response_model: type[StructuredT],
        payload: Mapping[str, Any],
        prompt_version: str,
    ) -> LLMGeneration[StructuredT]:
        serialized = json.dumps(
            payload,
            default=_json_default,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt = self._prompts.resolve(role, prompt_version).content
        schema = json.dumps(
            response_model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # UTF-8 bytes are a conservative hard upper bound for BPE-style token
        # counts: a token cannot consume less than one input byte.  This covers
        # the full request authored here (instructions + payload + schema),
        # including CJK and emoji without trusting a model-specific tokenizer.
        full_request_bytes = _PROVIDER_REQUEST_OVERHEAD_BYTES + sum(
            len(item.encode("utf-8")) for item in (prompt, serialized, schema)
        )
        reserved_input = max(1, full_request_bytes)
        reserved_output = _OUTPUT_RESERVATIONS.get(prompt_version, 6_000)
        if self._settings is not None:
            reserved_output = min(reserved_output, self._settings.max_output_tokens)
        call_id = str(uuid4())
        model = self._configured_model(role)
        self._store.reserve_llm_call(
            self._token,
            call_id=call_id,
            role=role.value,
            prompt_version=prompt_version,
            request_hash=canonical_json_hash(json.loads(serialized)),
            configured_model=model,
            reserved_input_tokens=reserved_input,
            reserved_output_tokens=reserved_output,
            max_calls_per_run=self._max_calls,
            max_input_tokens_per_run=self._max_input,
            max_output_tokens_per_run=self._max_output,
        )
        context = self._authority.issue(
            run_id=self._token.run_id,
            generation=self._token.generation,
            call_id=call_id,
            prompt_version=prompt_version,
            budget_reservation_id=call_id,
            reserved_output_tokens=reserved_output,
            is_cancelled=self._is_cancelled,
        )
        try:
            generation = await self._provider.generate(
                context=context,
                role=role,
                response_model=response_model,
                payload=payload,
            )
        except Exception as exc:
            status = "billing_unknown" if isinstance(exc, LLMProviderError) and exc.billing_unknown else "failed"
            try:
                self._store.finalize_llm_call(
                    self._token,
                    call_id=call_id,
                    status=status,
                    response_id=getattr(exc, "response_id", None),
                    error_type=type(exc).__name__,
                )
            except Exception:
                # The lease may have been cancelled or fenced while the remote
                # request was in flight.  The durable reservation remains as
                # evidence and no stale generation may publish a completion.
                pass
            raise
        metadata = generation.metadata
        self._store.finalize_llm_call(
            self._token,
            call_id=call_id,
            status="succeeded",
            actual_model=metadata.actual_model,
            response_id=metadata.response_id,
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
        )
        return generation

    def _configured_model(self, role: AgentRole) -> str:
        if self._settings is None or self._settings.provider_mode is ProviderMode.MOCK:
            return "deterministic-mock"
        return self._settings.model_for(role)

    def _is_cancelled(self) -> bool:
        try:
            self._store.assert_active_lease(self._token)
        except Exception:
            return True
        return False


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"unsupported LLM payload value: {type(value).__name__}")
