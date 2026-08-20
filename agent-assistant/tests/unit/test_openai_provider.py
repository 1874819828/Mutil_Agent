from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APITimeoutError, RateLimitError
from pydantic import BaseModel, ConfigDict

from app.config.llm_settings import LLMSettings, ProviderMode
from app.llm import (
    AgentRole,
    LLMCallAuthority,
    LLMCancelledError,
    LLMRateLimitError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    OpenAIResponsesProvider,
    PromptRegistry,
)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class FakeResponses:
    def __init__(self, effects: list[Any]) -> None:
        self.effects = effects
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


def _response(*, parsed: BaseModel | None = None) -> Any:
    return SimpleNamespace(
        id="resp_123",
        model="actual-model",
        output_parsed=parsed,
        output=[],
        status="completed",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18),
    )


def _settings(**overrides: Any) -> LLMSettings:
    values = {
        "provider_mode": ProviderMode.OPENAI,
        "api_key": "top-secret-key",
        "base_url": "https://example.invalid/v1",
        "default_model": "configured-model",
        "manager_model": None,
        "developer_model": None,
        "reviewer_model": None,
        "request_timeout_seconds": 30.0,
        "max_output_tokens": 1234,
        "max_429_retries": 2,
    }
    values.update(overrides)
    return LLMSettings.model_validate(values)


def _context(*, cancelled: Callable[[], bool] = lambda: False):
    return LLMCallAuthority().issue(
        run_id="run-1",
        generation=2,
        call_id="call-1",
        prompt_version="manager_plan/v1",
        budget_reservation_id="reservation-1",
        reserved_output_tokens=1234,
        is_cancelled=cancelled,
    )


def test_requests_strict_pydantic_output_without_storage_background_or_tools() -> None:
    responses = FakeResponses([_response(parsed=Answer(value="ok"))])
    client = SimpleNamespace(responses=responses)
    provider = OpenAIResponsesProvider(_settings(), client=client)

    result = asyncio.run(provider.generate(
        context=_context(),
        role=AgentRole.MANAGER,
        response_model=Answer,
        payload={"requirement": "add endpoint"},
    ))

    assert result.output == Answer(value="ok")
    assert result.metadata.response_id == "resp_123"
    assert result.metadata.configured_model == "configured-model"
    assert result.metadata.actual_model == "actual-model"
    assert result.metadata.input_tokens == 11
    assert result.metadata.output_tokens == 7
    request = responses.calls[0]
    assert request["text_format"] is Answer
    assert request["store"] is False
    assert request["background"] is False
    assert request["tools"] == []
    assert request["model"] == "configured-model"
    assert request["max_output_tokens"] == 1234
    assert "top-secret-key" not in str(request)


def test_role_specific_model_mapping() -> None:
    responses = FakeResponses([_response(parsed=Answer(value="ok"))])
    provider = OpenAIResponsesProvider(
        _settings(developer_model="coding-model"),
        client=SimpleNamespace(responses=responses),
    )

    asyncio.run(provider.generate(
        context=LLMCallAuthority().issue(
            run_id="run-1",
            generation=1,
            call_id="call-2",
            prompt_version="developer/v1",
            budget_reservation_id="reservation-2",
            reserved_output_tokens=100,
        ),
        role=AgentRole.DEVELOPER,
        response_model=Answer,
        payload={},
    ))
    assert responses.calls[0]["model"] == "coding-model"


def test_only_429_is_retried_locally() -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/responses")
    rate_response = httpx.Response(429, request=request, headers={"retry-after": "0"})
    responses = FakeResponses(
        [RateLimitError("limited", response=rate_response, body=None), _response(parsed=Answer(value="ok"))]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = OpenAIResponsesProvider(
        _settings(), client=SimpleNamespace(responses=responses), sleep=fake_sleep
    )
    result = asyncio.run(provider.generate(
        context=_context(), role=AgentRole.MANAGER, response_model=Answer, payload={}
    ))

    assert result.metadata.attempts == 2
    assert len(responses.calls) == 2
    assert sleeps == [0.0]


def test_429_stops_at_configured_limit() -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/responses")
    rate_response = httpx.Response(429, request=request, headers={"retry-after": "0"})
    responses = FakeResponses(
        [
            RateLimitError("limited", response=rate_response, body=None),
            RateLimitError("limited", response=rate_response, body=None),
        ]
    )
    provider = OpenAIResponsesProvider(
        _settings(max_429_retries=1),
        client=SimpleNamespace(responses=responses),
        sleep=lambda _: _noop(),
    )
    with pytest.raises(LLMRateLimitError) as caught:
        asyncio.run(provider.generate(
            context=_context(), role=AgentRole.MANAGER, response_model=Answer, payload={}
        ))
    assert caught.value.attempts == 2


def test_cancellation_during_rate_limit_backoff_prevents_second_request() -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/responses")
    rate_response = httpx.Response(429, request=request, headers={"retry-after": "0"})
    responses = FakeResponses(
        [
            RateLimitError("limited", response=rate_response, body=None),
            _response(parsed=Answer(value="unused")),
        ]
    )
    cancelled = False

    async def cancel_during_sleep(_: float) -> None:
        nonlocal cancelled
        cancelled = True

    provider = OpenAIResponsesProvider(
        _settings(max_429_retries=1),
        client=SimpleNamespace(responses=responses),
        sleep=cancel_during_sleep,
    )

    with pytest.raises(LLMCancelledError):
        asyncio.run(
            provider.generate(
                context=_context(cancelled=lambda: cancelled),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )

    assert len(responses.calls) == 1


async def _noop() -> None:
    return None


def test_timeout_is_not_retried_and_is_billing_unknown() -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/responses")
    responses = FakeResponses([APITimeoutError(request=request)])
    provider = OpenAIResponsesProvider(_settings(), client=SimpleNamespace(responses=responses))

    with pytest.raises(LLMTimeoutError) as caught:
        asyncio.run(provider.generate(
            context=_context(), role=AgentRole.MANAGER, response_model=Answer, payload={}
        ))
    assert caught.value.billing_unknown is True
    assert len(responses.calls) == 1


def test_missing_parsed_output_fails_closed() -> None:
    responses = FakeResponses([_response(parsed=None)])
    provider = OpenAIResponsesProvider(_settings(), client=SimpleNamespace(responses=responses))
    with pytest.raises(LLMStructuredOutputError):
        asyncio.run(provider.generate(
            context=_context(), role=AgentRole.MANAGER, response_model=Answer, payload={}
        ))


def test_cancelled_context_never_calls_remote_provider() -> None:
    responses = FakeResponses([_response(parsed=Answer(value="unused"))])
    provider = OpenAIResponsesProvider(_settings(), client=SimpleNamespace(responses=responses))
    with pytest.raises(LLMCancelledError):
        asyncio.run(provider.generate(
            context=_context(cancelled=lambda: True),
            role=AgentRole.MANAGER,
            response_model=Answer,
            payload={},
        ))
    assert responses.calls == []


def test_sdk_client_disables_automatic_retries() -> None:
    provider = OpenAIResponsesProvider(_settings())
    assert provider.sdk_max_retries == 0


def test_production_client_is_created_and_closed_inside_each_generate_event_loop() -> None:
    clients: list[Any] = []
    factory_kwargs: list[dict[str, Any]] = []

    class LoopBoundResponses:
        def __init__(self, owner: Any) -> None:
            self.owner = owner

        async def parse(self, **_: Any) -> Any:
            assert asyncio.get_running_loop() is self.owner.loop
            assert self.owner.closed is False
            return _response(parsed=Answer(value="ok"))

    class LoopBoundClient:
        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()
            self.closed = False
            self.close_loop: Any | None = None
            self.responses = LoopBoundResponses(self)

        async def aclose(self) -> None:
            assert asyncio.get_running_loop() is self.loop
            assert self.closed is False
            self.closed = True
            self.close_loop = asyncio.get_running_loop()

    def client_factory(**kwargs: Any) -> LoopBoundClient:
        factory_kwargs.append(kwargs)
        client = LoopBoundClient()
        clients.append(client)
        return client

    provider = OpenAIResponsesProvider(_settings(), client_factory=client_factory)

    first = asyncio.run(
        provider.generate(
            context=_context(),
            role=AgentRole.MANAGER,
            response_model=Answer,
            payload={"sequence": 1},
        )
    )
    second = asyncio.run(
        provider.generate(
            context=_context(),
            role=AgentRole.MANAGER,
            response_model=Answer,
            payload={"sequence": 2},
        )
    )

    assert first.output == second.output == Answer(value="ok")
    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert clients[0].loop is not clients[1].loop
    assert all(client.closed for client in clients)
    assert all(client.close_loop is client.loop for client in clients)
    assert len(factory_kwargs) == 2
    assert all(kwargs["max_retries"] == 0 for kwargs in factory_kwargs)
    assert all(kwargs["base_url"] == "https://example.invalid/v1" for kwargs in factory_kwargs)
    assert all(kwargs["api_key"] == "top-secret-key" for kwargs in factory_kwargs)


def test_prompt_registry_rejects_role_version_mismatch() -> None:
    registry = PromptRegistry()
    with pytest.raises(ValueError, match="does not belong"):
        registry.resolve(AgentRole.DEVELOPER, "manager_plan/v1")
