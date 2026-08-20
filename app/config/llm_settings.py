"""Environment-only settings for structured LLM providers."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

if TYPE_CHECKING:
    from app.llm.provider import AgentRole


class ProviderMode(str, Enum):
    MOCK = "mock"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class SettingsError(ValueError):
    pass


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_mode: ProviderMode = ProviderMode.MOCK
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str = "https://api.openai.com/v1"
    default_model: str | None = None
    manager_model: str | None = None
    developer_model: str | None = None
    reviewer_model: str | None = None
    request_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    max_output_tokens: int = Field(default=12_000, ge=1, le=100_000)
    max_429_retries: int = Field(default=2, ge=0, le=5)

    @model_validator(mode="after")
    def validate_remote_transport(self) -> "LLMSettings":
        if self.provider_mode is ProviderMode.MOCK:
            return self
        _require_secure_base_url(self.base_url)
        return self

    @model_validator(mode="after")
    def validate_remote_provider_requirements(self) -> "LLMSettings":
        if self.provider_mode in {ProviderMode.OPENAI, ProviderMode.DEEPSEEK}:
            provider_name = self.provider_mode.value.capitalize()
            if self.api_key is None or not self.api_key.get_secret_value().strip():
                raise SettingsError(f"{provider_name} provider requires an API key")
            if not self.default_model and not all(
                (self.manager_model, self.developer_model, self.reviewer_model)
            ):
                raise SettingsError(
                    f"{provider_name} provider requires a default model or all role models"
                )
        return self

    def model_for(self, role: "AgentRole") -> str:
        role_value = role.value
        role_model = {
            "manager": self.manager_model,
            "developer": self.developer_model,
            "reviewer": self.reviewer_model,
        }.get(role_value)
        model = role_model or self.default_model
        if not model:
            raise SettingsError(f"no model configured for role {role_value}")
        return model

    @classmethod
    def from_env(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        dotenv_path: Path | str = Path(".env"),
    ) -> "LLMSettings":
        # .env values are defaults; process environment wins.  Neither values
        # nor validation exceptions include secret contents.
        file_values = {
            key: str(value)
            for key, value in dotenv_values(dotenv_path).items()
            if value is not None
        }
        merged = {**file_values, **dict(os.environ if environ is None else environ)}

        def pick(*names: str) -> str | None:
            for name in names:
                value = merged.get(name)
                if value and value.strip():
                    return value.strip()
            return None

        provider_mode = pick("PROVIDER_MODE") or "mock"
        mode = provider_mode.casefold()
        if mode == ProviderMode.DEEPSEEK.value:
            api_key = pick("DEEPSEEK_API_KEY", "LLM_API_KEY")
            default_model = pick("DEEPSEEK_MODEL", "LLM_MODEL")
            role_models = (
                pick("DEEPSEEK_MANAGER_MODEL"),
                pick("DEEPSEEK_DEVELOPER_MODEL"),
                pick("DEEPSEEK_REVIEWER_MODEL"),
            )
            base_url = pick("DEEPSEEK_BASE_URL", "LLM_BASE_URL") or "https://api.deepseek.com"
            provider_name = "Deepseek"
        else:
            api_key = pick("OPENAI_API_KEY", "LLM_API_KEY")
            default_model = pick("OPENAI_MODEL", "LLM_MODEL")
            role_models = (
                pick("OPENAI_MANAGER_MODEL"),
                pick("OPENAI_DEVELOPER_MODEL"),
                pick("OPENAI_REVIEWER_MODEL"),
            )
            base_url = pick("OPENAI_BASE_URL", "LLM_BASE_URL") or "https://api.openai.com/v1"
            provider_name = "OpenAI"
        if mode in {ProviderMode.OPENAI.value, ProviderMode.DEEPSEEK.value} and not api_key:
            raise SettingsError(f"{provider_name} provider requires an API key")
        if (
            mode in {ProviderMode.OPENAI.value, ProviderMode.DEEPSEEK.value}
            and not default_model
            and not all(role_models)
        ):
            raise SettingsError(
                f"{provider_name} provider requires a default model or all role models"
            )
        if mode in {ProviderMode.OPENAI.value, ProviderMode.DEEPSEEK.value}:
            _require_secure_base_url(base_url)
        try:
            return cls.model_validate(
                {
                    "provider_mode": provider_mode,
                    "api_key": api_key,
                    "base_url": base_url,
                    "default_model": default_model,
                    "manager_model": role_models[0],
                    "developer_model": role_models[1],
                    "reviewer_model": role_models[2],
                    "request_timeout_seconds": pick(
                        "LLM_REQUEST_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"
                    )
                    or 120,
                    "max_output_tokens": pick("LLM_MAX_OUTPUT_TOKENS") or 12_000,
                    "max_429_retries": pick("LLM_MAX_429_RETRIES") or 2,
                }
            )
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("invalid LLM configuration") from exc


def _require_secure_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise SettingsError("remote LLM base URL must be an HTTPS origin without credentials")
