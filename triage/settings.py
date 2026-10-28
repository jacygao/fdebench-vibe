"""Environment-backed settings with secure provider URL validation."""

from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRIAGE_", env_file=".env", extra="ignore", frozen=True
    )

    llm_url: str = "https://api.openai.com/v1/chat/completions"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = Field(default="gpt-4.1-mini", min_length=1)
    llm_auth: Literal["bearer", "api-key"] = "bearer"
    llm_timeout_seconds: float = Field(default=8, ge=0.1, le=45)
    llm_attempts: int = Field(default=2, ge=1, le=3)
    request_timeout_seconds: float = Field(default=20, ge=0.1, le=50)
    max_concurrent_models: int = Field(default=16, ge=1, le=100)
    extract_model: str = Field(default="gpt-4.1-mini", min_length=1)
    extract_timeout_seconds: float = Field(default=45, ge=0.1, le=50)
    extract_model_timeout_seconds: float = Field(default=15, ge=0.1, le=45)
    max_concurrent_extractions: int = Field(default=4, ge=1, le=32)
    orchestrate_model: str = Field(default="gpt-4.1", min_length=1)
    orchestrate_timeout_seconds: float = Field(default=55, ge=2.1, le=57)
    orchestrate_model_timeout_seconds: float = Field(default=8, ge=0.1, le=20)
    orchestrate_max_rounds: int = Field(default=12, ge=1, le=30)
    orchestrate_max_steps: int = Field(default=64, ge=1, le=128)
    max_concurrent_workflows: int = Field(default=8, ge=1, le=32)
    tool_timeout_seconds: float = Field(default=5, ge=0.1, le=20)
    tool_attempts: int = Field(default=2, ge=1, le=3)
    tool_allowed_origins: list[str] = Field(default_factory=lambda: ["https://tools.fdebench.dev"])
    tool_private_origins: list[str] = Field(default_factory=list)
    tool_read_only_names: list[str] = Field(
        default_factory=lambda: [
            "crm_search",
            "crm_get_account",
            "subscription_check",
            "calendar_check",
            "inventory_query",
        ]
    )
    tool_idempotent_names: list[str] = Field(default_factory=list)
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    input_price_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_price_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("llm_url")
    @classmethod
    def validate_llm_url(cls, value: str) -> str:
        """Require TLS and disallow credentials or fragments in provider URLs."""
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("LLM URL must be an HTTPS URL without credentials or fragments")
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        """Accept Redis URLs while keeping any credentials out of settings representations."""
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.fragment:
            raise ValueError("Redis URL must use redis:// or rediss:// with a host")
        return value

    @field_validator("tool_allowed_origins", "tool_private_origins")
    @classmethod
    def validate_tool_origins(cls, values: list[str]) -> list[str]:
        """Require exact, canonical origins rather than wildcard hosts or URL prefixes."""
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
            ):
                raise ValueError("Tool origins must be exact http(s)://host[:port] values")
            # Accessing port also validates its range and syntax.
            _ = parsed.port
        return values

    @property
    def configured(self) -> bool:
        """Report whether a non-empty model API key is configured."""
        return bool(self.llm_api_key.get_secret_value().strip())
