"""DeepSeek Chat Completions adapter with fail-closed JSON validation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import json
import re
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
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from app.config.llm_settings import LLMSettings

from .prompts import PromptRegistry
from .provider import (
    AgentRole,
    LLMAuthenticationError,
    LLMCallContext,
    LLMCallMetadata,
    LLMCancelledError,
    LLMGeneration,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)


StructuredT = TypeVar("StructuredT", bound=BaseModel)
Sleep = Callable[[float], Awaitable[None]]
ClientFactory = Callable[..., Any]
_SAFE_PROVIDER_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class DeepSeekChatProvider:
    """DeepSeek implementation using its documented JSON Output path.

    DeepSeek V4 enables thinking by default.  Structured role calls disable it
    explicitly so ``max_tokens`` is available to the JSON contract rather than
    being consumed by hidden reasoning before the visible object is complete.
    """

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
            raise ValueError("DeepSeek provider requires an API key")
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
        schema = json.dumps(
            response_model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        system_prompt = _json_system_prompt(prompt.content, schema)
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
                    response = await client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": _serialize_payload(payload)},
                        ],
                        response_format={"type": "json_object"},
                        max_tokens=max_output_tokens,
                        temperature=0,
                        stream=False,
                        extra_body={"thinking": {"type": "disabled"}},
                    )
                    break
                except RateLimitError as exc:
                    if attempts > self._settings.max_429_retries:
                        raise LLMRateLimitError(
                            "DeepSeek rate limit retry budget exhausted",
                            attempts=attempts,
                            request_id=_request_id(exc),
                            status_code=429,
                        ) from exc
                    if context.cancelled:
                        raise LLMCancelledError(
                            "LLM call was cancelled while rate limited", attempts=attempts
                        ) from exc
                    await self._sleep(_retry_after_seconds(exc))
                except APITimeoutError as exc:
                    raise LLMTimeoutError(
                        "DeepSeek request timed out; billing state is unknown",
                        attempts=attempts,
                        billing_unknown=True,
                        request_id=_request_id(exc),
                    ) from exc
                except (AuthenticationError, PermissionDeniedError) as exc:
                    raise LLMAuthenticationError(
                        "DeepSeek rejected the configured credentials or permissions",
                        attempts=attempts,
                        request_id=_request_id(exc),
                        status_code=_status_code(exc),
                    ) from exc
                except (BadRequestError, APIConnectionError, APIStatusError, APIError, OpenAIError) as exc:
                    # Do not retry transport/5xx failures: the remote execution
                    # and billing state cannot be inferred safely.
                    status_code = _status_code(exc)
                    provider_code = _provider_code(exc)
                    raise LLMProviderError(
                        _safe_failure_message(status_code, provider_code),
                        attempts=attempts,
                        billing_unknown=status_code is None or status_code >= 500,
                        request_id=_request_id(exc),
                        status_code=status_code,
                        provider_code=provider_code,
                    ) from exc
        finally:
            if self._client is None:
                await _close_client(client)

        response_id = _safe_value(getattr(response, "id", None))
        if context.cancelled:
            raise LLMCancelledError(
                "LLM result arrived after cancellation and was discarded",
                attempts=attempts,
                response_id=response_id,
            )

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMStructuredOutputError(
                "DeepSeek returned no completion choice",
                attempts=attempts,
                response_id=response_id,
            )
        choice = choices[0]
        finish_reason = _string_attr(choice, "finish_reason")
        if finish_reason == "length":
            raise LLMStructuredOutputError(
                "DeepSeek JSON output reached the token limit and was incomplete",
                attempts=attempts,
                response_id=response_id,
            )
        if finish_reason == "content_filter":
            raise LLMRefusalError(
                "DeepSeek filtered the requested output",
                attempts=attempts,
                response_id=response_id,
            )
        if finish_reason == "insufficient_system_resource":
            raise LLMProviderError(
                "DeepSeek generation stopped because provider capacity was insufficient",
                attempts=attempts,
                billing_unknown=True,
                response_id=response_id,
                provider_code=finish_reason,
            )
        if finish_reason != "stop":
            raise LLMStructuredOutputError(
                f"DeepSeek returned unexpected finish reason: {_safe_value(finish_reason) or 'unknown'}",
                attempts=attempts,
                response_id=response_id,
            )

        message = getattr(choice, "message", None)
        if getattr(message, "tool_calls", None):
            raise LLMStructuredOutputError(
                "DeepSeek returned an unexpected tool call for a structured-output request",
                attempts=attempts,
                response_id=response_id,
            )
        content = getattr(message, "content", None) if message is not None else None
        if not isinstance(content, str) or not content.strip():
            raise LLMStructuredOutputError(
                "DeepSeek returned no structured output",
                attempts=attempts,
                response_id=response_id,
            )
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMStructuredOutputError(
                "DeepSeek returned invalid JSON",
                attempts=attempts,
                response_id=response_id,
            ) from exc
        try:
            output = response_model.model_validate(decoded)
        except ValidationError as exc:
            raise LLMStructuredOutputError(
                "DeepSeek output failed local schema validation",
                attempts=attempts,
                response_id=response_id,
            ) from exc

        usage = getattr(response, "usage", None)
        input_tokens = _int_attr(usage, "prompt_tokens")
        output_tokens = _int_attr(usage, "completion_tokens")
        total_tokens = _int_attr(usage, "total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return LLMGeneration(
            output=output,
            metadata=LLMCallMetadata(
                provider="deepseek",
                configured_model=model,
                actual_model=_safe_value(getattr(response, "model", None)),
                response_id=response_id,
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


def _json_system_prompt(prompt: str, schema: str) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        "Return exactly one valid JSON object. Do not use Markdown fences, comments, "
        "or explanatory text. The JSON must satisfy this complete schema.\n"
        f"JSON_SCHEMA:\n{schema}"
    )


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


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _request_id(exc: BaseException) -> str | None:
    return _safe_value(getattr(exc, "request_id", None))


def _provider_code(exc: BaseException) -> str | None:
    body = getattr(exc, "body", None)
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    if isinstance(error, Mapping):
        return _safe_value(error.get("code") or error.get("type"))
    return _safe_value(body.get("code") or body.get("type"))


def _safe_failure_message(status_code: int | None, provider_code: str | None) -> str:
    details: list[str] = []
    if status_code is not None:
        details.append(f"HTTP {status_code}")
    if provider_code is not None:
        details.append(f"code={provider_code}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"DeepSeek provider request failed{suffix}"


def _safe_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if _SAFE_PROVIDER_VALUE.fullmatch(text) else None


def _string_attr(value: Any, name: str) -> str | None:
    item = getattr(value, name, None)
    return str(item) if item is not None else None


def _int_attr(value: Any, name: str) -> int | None:
    item = getattr(value, name, None) if value is not None else None
    return int(item) if item is not None else None
