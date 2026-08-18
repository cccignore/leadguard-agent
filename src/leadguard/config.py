from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Secrets are never included in API responses or logs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "LeadGuard Agent Lab"
    llm_provider: Literal["openai_compatible", "gemini"] = "openai_compatible"
    llm_api_base: str = "https://api.openlux.ai/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=160)
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    database_path: Path = Path("data/leadguard.db")
    rate_limit_seconds: int = Field(default=60, ge=60, le=3600)
    max_customer_message_chars: int = Field(default=2_000, ge=1, le=20_000)
    max_reply_chars: int = Field(default=320, ge=40, le=2_000)
    model_retry_attempts: int = Field(default=2, ge=1, le=4)
    model_timeout_seconds: float = Field(default=60.0, ge=5.0, le=180.0)

    @field_validator("llm_api_base")
    @classmethod
    def validate_llm_api_base(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LLM_API_BASE must be a credential-free absolute URL")
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise ValueError("LLM_API_BASE must use HTTPS outside localhost")
        return normalized

    @property
    def llm_configured(self) -> bool:
        key = self.active_api_key
        return bool(key and key.get_secret_value().strip())

    @property
    def active_api_key(self) -> SecretStr | None:
        if self.llm_provider == "gemini":
            return self.gemini_api_key
        return self.llm_api_key

    @property
    def active_model(self) -> str:
        if self.llm_provider == "gemini":
            return self.gemini_model
        return self.llm_model
