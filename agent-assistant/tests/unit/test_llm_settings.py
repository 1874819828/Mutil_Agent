from __future__ import annotations

from pathlib import Path

import pytest

from app.config.llm_settings import LLMSettings, ProviderMode, SettingsError
from app.llm import AgentRole


def test_loads_legacy_values_from_dotenv_and_maps_roles(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "PROVIDER_MODE=openai\n"
        "LLM_BASE_URL=https://example.invalid/v1\n"
        "LLM_API_KEY=secret-value\n"
        "LLM_MODEL=shared-model\n"
        "OPENAI_DEVELOPER_MODEL=coding-model\n",
        encoding="utf-8",
    )

    settings = LLMSettings.from_env(environ={}, dotenv_path=dotenv)

    assert settings.provider_mode is ProviderMode.OPENAI
    assert settings.base_url == "https://example.invalid/v1"
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "secret-value"
    assert settings.model_for(AgentRole.MANAGER) == "shared-model"
    assert settings.model_for(AgentRole.DEVELOPER) == "coding-model"
    assert "secret-value" not in repr(settings)


def test_openai_names_override_legacy_names(tmp_path: Path) -> None:
    settings = LLMSettings.from_env(
        environ={
            "PROVIDER_MODE": "openai",
            "LLM_BASE_URL": "https://legacy.invalid/v1",
            "OPENAI_BASE_URL": "https://preferred.invalid/v1",
            "LLM_API_KEY": "legacy",
            "OPENAI_API_KEY": "preferred",
            "LLM_MODEL": "legacy-model",
            "OPENAI_MODEL": "preferred-model",
        },
        dotenv_path=tmp_path / "missing",
    )

    assert settings.base_url == "https://preferred.invalid/v1"
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "preferred"
    assert settings.model_for(AgentRole.REVIEWER) == "preferred-model"


def test_openai_mode_requires_key_and_model(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="API key"):
        LLMSettings.from_env(
            environ={"PROVIDER_MODE": "openai", "LLM_MODEL": "model"},
            dotenv_path=tmp_path / "missing",
        )

    with pytest.raises(SettingsError, match="model"):
        LLMSettings.from_env(
            environ={"PROVIDER_MODE": "openai", "LLM_API_KEY": "secret"},
            dotenv_path=tmp_path / "missing",
        )


def test_mock_mode_does_not_require_credentials(tmp_path: Path) -> None:
    settings = LLMSettings.from_env(
        environ={"PROVIDER_MODE": "mock"},
        dotenv_path=tmp_path / "missing",
    )
    assert settings.provider_mode is ProviderMode.MOCK


def test_deepseek_mode_loads_legacy_llm_values(tmp_path: Path) -> None:
    settings = LLMSettings.from_env(
        environ={
            "PROVIDER_MODE": "deepseek",
            "LLM_BASE_URL": "https://api.deepseek.com",
            "LLM_API_KEY": "deepseek-secret",
            "LLM_MODEL": "deepseek-chat",
        },
        dotenv_path=tmp_path / "missing",
    )

    assert settings.provider_mode is ProviderMode.DEEPSEEK
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "deepseek-secret"
    assert settings.model_for(AgentRole.MANAGER) == "deepseek-chat"
    assert "deepseek-secret" not in repr(settings)


def test_deepseek_mode_requires_key_and_model(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="API key"):
        LLMSettings.from_env(
            environ={"PROVIDER_MODE": "deepseek", "LLM_MODEL": "deepseek-chat"},
            dotenv_path=tmp_path / "missing",
        )


def test_remote_provider_rejects_insecure_or_credentialed_base_url(tmp_path: Path) -> None:
    for base_url in (
        "http://api.deepseek.com",
        "https://user:password@api.deepseek.com",
        "not-a-url",
    ):
        with pytest.raises(SettingsError, match="HTTPS origin"):
            LLMSettings.from_env(
                environ={
                    "PROVIDER_MODE": "deepseek",
                    "LLM_API_KEY": "secret",
                    "LLM_MODEL": "deepseek-v4-flash",
                    "LLM_BASE_URL": base_url,
                },
                dotenv_path=tmp_path / "missing",
            )

    with pytest.raises(SettingsError, match="model"):
        LLMSettings.from_env(
            environ={"PROVIDER_MODE": "deepseek", "LLM_API_KEY": "secret"},
            dotenv_path=tmp_path / "missing",
        )
