"""Validated target-side log-agent configuration."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class AgentConfigError(RuntimeError):
    """The agent configuration could not be read or validated."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgentSettings(_StrictModel):
    state_dir: Path = Path("/var/lib/botman-log-agent/state")
    resume_max_age_sec: int = Field(default=300, ge=1, le=86_400)
    discord_message_limit: int = Field(default=2000, ge=256, le=2000)
    request_timeout_sec: float = Field(default=30.0, ge=1.0, le=120.0)
    retry_initial_sec: float = Field(default=1.0, ge=0.05, le=60.0)
    retry_max_sec: float = Field(default=30.0, ge=0.1, le=300.0)

    @field_validator("state_dir")
    @classmethod
    def validate_state_dir(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("state_dir must be absolute")
        return value


class AgentApp(_StrictModel):
    identifier: str
    enabled: bool = False
    webhook_url: str | None = Field(default=None, repr=False)
    thread_id: str | None = None
    subscription_id: str | None = None

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value or len(value) > 128 or any(ch in value for ch in "\x00\r\n"):
            raise ValueError("identifier must be a non-empty journald identifier")
        return value

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or any(ch in value for ch in "\x00\r\n"):
            raise ValueError("webhook_url must be an HTTPS URL")
        return value

    @field_validator("thread_id")
    @classmethod
    def validate_thread_id(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("thread_id must be a numeric Discord ID")
        return value

    @field_validator("subscription_id")
    @classmethod
    def validate_subscription_id(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(ch.isspace() for ch in value)):
            raise ValueError("subscription_id must not contain whitespace")
        return value

    @model_validator(mode="after")
    def validate_enabled_destination(self) -> "AgentApp":
        if self.enabled and not (self.webhook_url and self.thread_id and self.subscription_id):
            raise ValueError(
                "enabled live logs require webhook_url, thread_id, and subscription_id"
            )
        return self


class AgentConfig(_StrictModel):
    settings: AgentSettings = Field(default_factory=AgentSettings)
    apps: dict[str, AgentApp] = Field(default_factory=dict)

    @field_validator("apps")
    @classmethod
    def validate_app_names(cls, value: dict[str, AgentApp]) -> dict[str, AgentApp]:
        for name in value:
            if not name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-." for ch in name):
                raise ValueError(f"unsafe app key: {name!r}")
        return value


def load_agent_config(path: str | Path) -> AgentConfig:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AgentConfigError(f"failed to read/parse {source}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AgentConfigError("top-level agent config must be a YAML mapping")
    try:
        return AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise AgentConfigError(f"invalid agent config {source}:\n{exc}") from exc
