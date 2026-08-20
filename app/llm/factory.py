"""LLM provider construction from validated settings."""

from __future__ import annotations

from app.config.llm_settings import LLMSettings, ProviderMode

from .deepseek_provider import DeepSeekChatProvider
from .openai_provider import OpenAIResponsesProvider
from .provider import DeterministicMockProvider, LLMProvider


def create_llm_provider(settings: LLMSettings) -> LLMProvider:
    if settings.provider_mode is ProviderMode.MOCK:
        return DeterministicMockProvider()
    if settings.provider_mode is ProviderMode.OPENAI:
        return OpenAIResponsesProvider(settings)
    if settings.provider_mode is ProviderMode.DEEPSEEK:
        return DeepSeekChatProvider(settings)
    raise ValueError(f"unsupported provider mode: {settings.provider_mode}")
