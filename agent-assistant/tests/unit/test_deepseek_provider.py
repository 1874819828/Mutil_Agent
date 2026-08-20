from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from openai import APITimeoutError, BadRequestError, RateLimitError
from pydantic import BaseModel, ConfigDict

from app.config.llm_settings import LLMSettings, ProviderMode
from app.llm import (
    AgentRole,
    DeepSeekChatProvider,
    LLMCallAuthority,
    LLMCancelledError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)
from app.llm.factory import create_llm_provider


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class FakeChatCompletions:
    def __init__(self, effects: list[Any]) -> None:
        self.effects = effects
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class ForbiddenResponses:
    async def parse(self, **_: Any) -> Any:
        raise AssertionError("DeepSeek must not use the OpenAI Responses API")


def _client(effects: list[Any]) -> tuple[Any, FakeChatCompletions]:
    completions = FakeChatCompletions(effects)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        responses=ForbiddenResponses(),
    )
    return client, completions


def _response(
    content: str | None,
    *,
    finish_reason: str = "stop",
    tool_calls: list[Any] | None = None,
) -> Any:
    return SimpleNamespace(
        id="chatcmpl_123",
        model="actual-model",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content=content,
                    refusal=None,
                    tool_calls=tool_calls,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        ),
    )


def _settings(**overrides: Any) -> LLMSettings:
    values = {
        "provider_mode": ProviderMode.DEEPSEEK,
        "api_key": "top-secret-key",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
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


def test_uses_chat_completions_json_mode_and_embeds_complete_schema() -> None:
    client, completions = _client([_response('{"value":"ok"}')])
    provider = DeepSeekChatProvider(_settings(), client=client)

    result = asyncio.run(
        provider.generate(
            context=_context(),
            role=AgentRole.MANAGER,
            response_model=Answer,
            payload={"requirement": "add endpoint"},
        )
    )

    assert result.output == Answer(value="ok")
    assert result.metadata.provider == "deepseek"
    assert result.metadata.response_id == "chatcmpl_123"
    assert result.metadata.configured_model == "deepseek-chat"
    assert result.metadata.actual_model == "actual-model"
    assert result.metadata.input_tokens == 11
    assert result.metadata.output_tokens == 7

    assert len(completions.calls) == 1
    request = completions.calls[0]
    assert request["model"] == "deepseek-chat"
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["max_tokens"] == 1234
    assert request["temperature"] == 0
    assert "text_format" not in request
    assert "top-secret-key" not in str(request)

    messages = request["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    system_prompt = messages[0]["content"]
    assert "json" in system_prompt.casefold()
    schema_marker = "JSON_SCHEMA:\n"
    assert schema_marker in system_prompt
    embedded_schema = json.loads(system_prompt.rsplit(schema_marker, 1)[1])
    assert embedded_schema == Answer.model_json_schema()
    assert json.loads(messages[1]["content"]) == {"requirement": "add endpoint"}


def test_locally_validates_json_against_pydantic_schema() -> None:
    client, _ = _client([_response('{"value":"ok","unexpected":true}')])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMStructuredOutputError, match="schema validation"):
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_content_fails_closed(content: str | None) -> None:
    client, _ = _client([_response(content)])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMStructuredOutputError, match="no structured output"):
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )


def test_invalid_json_fails_closed() -> None:
    client, _ = _client([_response('{"value":')])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMStructuredOutputError, match="invalid JSON"):
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )


def test_length_finish_reason_is_reported_before_parsing_partial_json() -> None:
    client, _ = _client([_response('{"value":"partial', finish_reason="length")])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMStructuredOutputError, match="token limit") as caught:
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )
    assert caught.value.response_id == "chatcmpl_123"


def test_content_filter_finish_reason_fails_as_refusal() -> None:
    client, _ = _client([_response(None, finish_reason="content_filter")])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMRefusalError, match="filtered") as caught:
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )
    assert caught.value.response_id == "chatcmpl_123"


def test_insufficient_system_resource_fails_with_unknown_billing() -> None:
    client, _ = _client(
        [_response(None, finish_reason="insufficient_system_resource")]
    )
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMProviderError, match="capacity") as caught:
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )
    assert caught.value.billing_unknown is True
    assert caught.value.provider_code == "insufficient_system_resource"
    assert caught.value.response_id == "chatcmpl_123"


def test_unexpected_tool_calls_fail_closed_even_with_valid_json_content() -> None:
    tool_call = SimpleNamespace(
        id="call_123",
        type="function",
        function=SimpleNamespace(name="unexpected", arguments="{}"),
    )
    client, _ = _client([_response('{"value":"ok"}', tool_calls=[tool_call])])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMStructuredOutputError, match="tool") as caught:
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )
    assert caught.value.response_id == "chatcmpl_123"


def test_timeout_is_not_retried_and_does_not_echo_sdk_details() -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    client, completions = _client([APITimeoutError(request=request)])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMTimeoutError) as caught:
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )
    assert caught.value.billing_unknown is True
    assert caught.value.attempts == 1
    assert len(completions.calls) == 1
    assert "api.deepseek.com" not in str(caught.value)


def test_rate_limit_error_is_retried_without_echoing_sensitive_sdk_message() -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(429, request=request, headers={"retry-after": "0"})
    sdk_error = RateLimitError(
        "raw secret sk-sensitive-rate-limit-message",
        response=response,
        body={"error": {"message": "body secret sk-sensitive-body"}},
    )
    client, completions = _client([sdk_error])
    provider = DeepSeekChatProvider(
        _settings(max_429_retries=0),
        client=client,
    )

    with pytest.raises(LLMRateLimitError) as caught:
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )
    assert caught.value.attempts == 1
    assert len(completions.calls) == 1
    assert "sk-sensitive" not in str(caught.value)
    assert "body secret" not in str(caught.value)


def test_cancellation_during_rate_limit_backoff_prevents_second_request() -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(429, request=request, headers={"retry-after": "0"})
    sdk_error = RateLimitError("limited", response=response, body=None)
    client, completions = _client([sdk_error, _response('{"value":"unused"}')])
    cancelled = False

    async def cancel_during_sleep(_: float) -> None:
        nonlocal cancelled
        cancelled = True

    provider = DeepSeekChatProvider(
        _settings(max_429_retries=1),
        client=client,
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

    assert len(completions.calls) == 1


def test_provider_error_exposes_only_allowlisted_diagnostics() -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(400, request=request)
    sdk_error = BadRequestError(
        "raw secret sk-sensitive-provider-message",
        response=response,
        body={
            "error": {
                "message": "body secret sk-sensitive-provider-body",
                "code": "invalid_request",
            }
        },
    )
    client, completions = _client([sdk_error])
    provider = DeepSeekChatProvider(_settings(), client=client)

    with pytest.raises(LLMProviderError) as caught:
        asyncio.run(
            provider.generate(
                context=_context(),
                role=AgentRole.MANAGER,
                response_model=Answer,
                payload={},
            )
        )
    assert caught.value.status_code == 400
    assert caught.value.provider_code == "invalid_request"
    assert caught.value.billing_unknown is False
    assert caught.value.attempts == 1
    assert len(completions.calls) == 1
    assert str(caught.value) == (
        "DeepSeek provider request failed (HTTP 400, code=invalid_request)"
    )
    assert "sk-sensitive" not in str(caught.value)
    assert "body secret" not in str(caught.value)


def test_factory_selects_deepseek_chat_provider() -> None:
    provider = create_llm_provider(_settings())

    assert isinstance(provider, DeepSeekChatProvider)


def test_production_client_is_created_and_closed_inside_each_generate_event_loop() -> None:
    clients: list[Any] = []
    factory_kwargs: list[dict[str, Any]] = []

    class LoopBoundCompletions:
        def __init__(self, owner: Any) -> None:
            self.owner = owner

        async def create(self, **_: Any) -> Any:
            assert asyncio.get_running_loop() is self.owner.loop
            assert self.owner.closed is False
            return _response('{"value":"ok"}')

    class LoopBoundClient:
        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()
            self.closed = False
            self.close_loop: Any | None = None
            self.chat = SimpleNamespace(completions=LoopBoundCompletions(self))

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

    provider = DeepSeekChatProvider(_settings(), client_factory=client_factory)

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
    assert all(kwargs["base_url"] == "https://api.deepseek.com" for kwargs in factory_kwargs)
    assert all(kwargs["api_key"] == "top-secret-key" for kwargs in factory_kwargs)
