"""Official OpenAI Responses API adapter with strict structured outputs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import json
import time
from typing import Any, TypeVar

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel

from app.config.llm_settings import LLMSettings

from .prompts import PromptRegistry
from .provider import (
    AgentRole,
    LLMAuthenticationError,
    LLMCallContext,
    LLMCancelledError,
    LLMGeneration,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    LLMCallMetadata,
)


StructuredT = TypeVar("StructuredT", bound=BaseModel)
Sleep = Callable[[float], Awaitable[None]]
ClientFactory = Callable[..., Any]


class OpenAIResponsesProvider:
    """OpenAI implementation with SDK retries disabled and fail-closed output."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        client: Any | None = None,
        client_factory: ClientFactory | None = None,
        prompts: PromptRegistry | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._prompts = prompts or PromptRegistry()
        self._sleep = sleep
        if client is not None and client_factory is not None:
            raise ValueError("provide either client or client_factory, not both")
        if settings.api_key is None and client is None and client_factory is None:
            raise ValueError("OpenAI provider requires an API key")
        self._client = client
        self._client_factory = client_factory or AsyncOpenAI

    @property
    def sdk_max_retries(self) -> int:
        if self._client is None:
            return 0
        value = getattr(self._client, "max_retries", None)
        if value is None:
            value = getattr(self._client, "_max_retries", None)
        return int(value) if value is not None else 0

    async def generate(
        self,
        *,
        context: LLMCallContext,
        role: AgentRole,
        response_model: type[StructuredT],
        payload: Mapping[str, Any],
    ) -> LLMGeneration[StructuredT]:
        context.assert_authorized()
        if context.cancelled:
            raise LLMCancelledError("LLM call was cancelled before provider request")
        prompt = self._prompts.resolve(role, context.prompt_version)
        model = self._settings.model_for(role)
        max_output_tokens = min(
            self._settings.max_output_tokens, context.reserved_output_tokens
        )
        started = time.perf_counter()
        attempts = 0
        client = self._client or self._client_factory(**self._client_kwargs())
        try:
            while True:
                if context.cancelled:
                    raise LLMCancelledError(
                        "LLM call was cancelled before provider retry",
                        attempts=attempts,
                    )
                attempts += 1
                try:
                    response = await client.responses.parse(
                        model=model,
                        instructions=prompt.content,
                        input=_serialize_payload(payload),
                        text_format=response_model,
                        max_output_tokens=max_output_tokens,
                        store=False,
                        background=False,
                        tools=[],
                        metadata={
                            "run_id": context.run_id,
                            "generation": str(context.generation),
                            "call_id": context.call_id,
                            "prompt_version": context.prompt_version,
                        },
                    )
                    break
                except RateLimitError as exc:
                    if attempts > self._settings.max_429_retries:
                        raise LLMRateLimitError(
                            "LLM provider rate limit retry budget exhausted", attempts=attempts
                        ) from exc
                    if context.cancelled:
                        raise LLMCancelledError(
                            "LLM call was cancelled while rate limited", attempts=attempts
                        ) from exc
                    await self._sleep(_retry_after_seconds(exc))
                except APITimeoutError as exc:
                    raise LLMTimeoutError(
                        "LLM provider request timed out; billing state is unknown",
                        attempts=attempts,
                        billing_unknown=True,
                    ) from exc
                except (AuthenticationError, PermissionDeniedError) as exc:
                    raise LLMAuthenticationError(
                        "LLM provider rejected the configured credentials or permissions",
                        attempts=attempts,
                    ) from exc
                except (BadRequestError, APIConnectionError, APIStatusError, APIError) as exc:
                    # No automatic retries: for transport errors and 5xx it is
                    # unsafe to assume the provider did not execute or bill.
                    status_code = getattr(exc, "status_code", None)
                    billing_unknown = status_code is None or int(status_code) >= 500
                    raise LLMProviderError(
                        "LLM provider request failed",
                        attempts=attempts,
                        billing_unknown=billing_unknown,
                    ) from exc
        finally:
            if self._client is None:
                await _close_client(client)

        if context.cancelled:
            raise LLMCancelledError(
                "LLM result arrived after cancellation and was discarded",
                attempts=attempts,
                response_id=_string_attr(response, "id"),
            )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            if _contains_refusal(response):
                raise LLMRefusalError(
                    "LLM provider refused the request",
                    attempts=attempts,
                    response_id=_string_attr(response, "id"),
                )
            raise LLMStructuredOutputError(
                "LLM provider returned no validated structured output",
                attempts=attempts,
                response_id=_string_attr(response, "id"),
            )
        try:
            output = (
                parsed
                if isinstance(parsed, response_model)
                else response_model.model_validate(parsed)
            )
        except Exception as exc:
            raise LLMStructuredOutputError(
                "LLM provider output failed local schema validation",
                attempts=attempts,
                response_id=_string_attr(response, "id"),
            ) from exc
        usage = getattr(response, "usage", None)
        input_tokens = _int_attr(usage, "input_tokens")
        output_tokens = _int_attr(usage, "output_tokens")
        total_tokens = _int_attr(usage, "total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return LLMGeneration(
            output=output,
            metadata=LLMCallMetadata(
                provider="openai",
                configured_model=model,
                actual_model=_string_attr(response, "model"),
                response_id=_string_attr(response, "id"),
                prompt_version=prompt.version,
                prompt_sha256=prompt.sha256,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                attempts=attempts,
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            ),
        )

    def _client_kwargs(self) -> dict[str, Any]:
        assert self._settings.api_key is not None
        return {
            "api_key": self._settings.api_key.get_secret_value(),
            "base_url": self._settings.base_url,
            "timeout": self._settings.request_timeout_seconds,
            "max_retries": 0,
        }


async def _close_client(client: Any) -> None:
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if hasattr(result, "__await__"):
        await result


def _serialize_payload(payload: Mapping[str, Any]) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        raise TypeError(f"unsupported payload value {type(value).__name__}")

    return json.dumps(payload, default=default, ensure_ascii=False, separators=(",", ":"))


def _retry_after_seconds(exc: RateLimitError) -> float:
    raw = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return min(30.0, max(0.0, float(raw))) if raw is not None else 1.0
    except ValueError:
        return 1.0


def _contains_refusal(response: Any) -> bool:
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                return True
    return False


def _string_attr(value: Any, name: str) -> str | None:
    item = getattr(value, name, None)
    return str(item) if item is not None else None


def _int_attr(value: Any, name: str) -> int | None:
    item = getattr(value, name, None) if value is not None else None
    return int(item) if item is not None else None
