"""Validated configuration models and derived filesystem paths."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
LOG_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
LINUX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")
JOURNAL_SIZE_RE = re.compile(r"^[1-9][0-9]*(?:K|M|G|T|P)?$")
GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

STACK_BASE = PurePosixPath("/srv/botman/stacks")


def _validate_safe_name(value: str, *, label: str) -> str:
    if not SAFE_NAME_RE.fullmatch(value):
        raise ValueError(
            f"{label} must match {SAFE_NAME_RE.pattern!r}; use lowercase letters, digits, and '-'"
        )
    return value


def _validate_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    if not argv:
        raise ValueError("compose_argv must contain at least one argument")
    for arg in argv:
        if not arg or "\x00" in arg or "\n" in arg or "\r" in arg:
            raise ValueError("compose_argv arguments must be non-empty and contain no NUL/newlines")
    return argv


def _validate_absolute_path(value: Path | None, *, label: str) -> Path | None:
    if value is None:
        return None
    if not value.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return value


def _validate_relative_posix_path(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not value or "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe relative POSIX path without traversal")
    return str(path)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)


class SettingsConfig(StrictModel):
    timezone: str = "Europe/Istanbul"
    command_prefix: str = "!"
    journal_max_use: str = "1G"
    live_resume_max_age_sec: int = Field(default=300, ge=1, le=86_400)
    release_keep_count: int = Field(default=3, ge=1, le=20)
    git_known_hosts: Path = Path("/etc/botman/git_known_hosts")
    repo_cache_root: Path = Path("/var/lib/botman/repos")
    deploy_key_root: Path = Path("/var/lib/botman/keys")
    log_export_bin: Path = Path("/opt/botman-agent-venv/bin/botman-log-export")
    agent_config_path: Path = Path("/var/lib/botman-log-agent/config.yaml")

    @field_validator(
        "git_known_hosts",
        "repo_cache_root",
        "deploy_key_root",
        "log_export_bin",
        "agent_config_path",
    )
    @classmethod
    def validate_central_paths(cls, value: Path) -> Path:
        validated = _validate_absolute_path(value, label="central Botman path")
        assert validated is not None
        return validated

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("command_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if not value or len(value) > 5 or any(ch.isspace() for ch in value):
            raise ValueError("command_prefix must be 1-5 non-whitespace characters")
        return value

    @field_validator("journal_max_use")
    @classmethod
    def validate_journal_size(cls, value: str) -> str:
        value = value.upper()
        if not JOURNAL_SIZE_RE.fullmatch(value):
            raise ValueError("journal_max_use must look like 512M, 1G, or another positive size")
        return value


class _ServerBase(StrictModel):
    compose_argv: tuple[str, ...] = ("docker", "compose")

    @field_validator("compose_argv")
    @classmethod
    def validate_compose_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value)


class LocalServerConfig(_ServerBase):
    type: Literal["local"] = "local"


class SSHServerConfig(_ServerBase):
    type: Literal["ssh"] = "ssh"
    host: str
    port: int = Field(default=22, ge=1, le=65_535)
    user: str
    key: Path
    known_hosts: Path | None = None
    connect_timeout_sec: float = Field(default=15.0, ge=1.0, le=120.0)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value or any(ch.isspace() for ch in value) or any(ch in value for ch in "\x00\r\n"):
            raise ValueError("host must be a non-empty hostname or address without whitespace")
        return value

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        if not LINUX_USER_RE.fullmatch(value):
            raise ValueError("SSH user must be a conservative Linux username")
        return value

    @field_validator("key")
    @classmethod
    def validate_key_path(cls, value: Path) -> Path:
        validated = _validate_absolute_path(value, label="SSH key")
        assert validated is not None
        return validated

    @field_validator("known_hosts")
    @classmethod
    def validate_known_hosts_path(cls, value: Path | None) -> Path | None:
        return _validate_absolute_path(value, label="known_hosts")


ServerConfig = Annotated[LocalServerConfig | SSHServerConfig, Field(discriminator="type")]


class GitConfig(StrictModel):
    repo_url: str
    branch: str = "main"
    deploy_key_path: Path | None = None

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        if (
            not value
            or len(value) > 2048
            or value.startswith("-")
            or any(ch.isspace() for ch in value)
            or any(ch in value for ch in "\x00\r\n")
        ):
            raise ValueError("repo_url must be a non-empty SSH Git URL without whitespace/control characters")
        parsed = urlparse(value)
        ssh_url = parsed.scheme == "ssh" and bool(parsed.hostname) and bool(parsed.path)
        scp_like = bool(re.fullmatch(r"[A-Za-z0-9._-]+@[^:/\s]+:.+", value))
        if not (ssh_url or scp_like):
            raise ValueError("repo_url must use SSH (ssh://... or user@host:path)")
        return value

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        if (
            not GIT_BRANCH_RE.fullmatch(value)
            or value.startswith("-")
            or value.endswith(("/", ".", ".lock"))
            or ".." in value
            or "//" in value
            or "@{" in value
        ):
            raise ValueError("branch is not a safe Git branch/ref name")
        return value

    @field_validator("deploy_key_path")
    @classmethod
    def validate_deploy_key_path(cls, value: Path | None) -> Path | None:
        return _validate_absolute_path(value, label="deploy_key_path")


class LogConfig(StrictModel):
    webhook_id: str | None = None
    webhook_url: str | None = Field(default=None, repr=False)
    live_enabled: bool = False
    thread_id: str | None = None
    subscription_id: str | None = None

    @field_validator("webhook_id", "thread_id")
    @classmethod
    def validate_discord_id(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("Discord IDs must be numeric strings")
        return value

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or "\x00" in value:
            raise ValueError("webhook_url must be an HTTPS URL")
        return value

    @field_validator("subscription_id")
    @classmethod
    def validate_subscription_id(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(ch.isspace() for ch in value)):
            raise ValueError("subscription_id must not contain whitespace")
        return value

    @model_validator(mode="after")
    def validate_live_state(self) -> "LogConfig":
        if self.live_enabled and not (self.webhook_url and self.thread_id and self.subscription_id):
            raise ValueError(
                "live_enabled requires webhook_url, thread_id, and subscription_id"
            )
        return self


class AppConfig(StrictModel):
    service: str
    env_file: str | None = None
    log_identifier: str
    git: GitConfig
    log: LogConfig = Field(default_factory=LogConfig)

    @field_validator("service")
    @classmethod
    def validate_service(cls, value: str) -> str:
        if not SERVICE_RE.fullmatch(value):
            raise ValueError("service must be a conservative Compose service name")
        return value

    @field_validator("env_file")
    @classmethod
    def validate_env_file(cls, value: str | None) -> str | None:
        return _validate_relative_posix_path(value, label="env_file")

    @field_validator("log_identifier")
    @classmethod
    def validate_log_identifier(cls, value: str) -> str:
        if not LOG_IDENTIFIER_RE.fullmatch(value):
            raise ValueError("log_identifier contains unsupported characters")
        return value


class StackConfig(StrictModel):
    server: str
    channel_id: str
    project_name: str
    compose_file: str = "compose.yml"
    apps: dict[str, AppConfig] = Field(default_factory=dict)

    @field_validator("server")
    @classmethod
    def validate_server_name(cls, value: str) -> str:
        return _validate_safe_name(value, label="server reference")

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("channel_id must be a numeric Discord ID string")
        return value

    @field_validator("project_name")
    @classmethod
    def validate_project_name(cls, value: str) -> str:
        if not PROJECT_RE.fullmatch(value):
            raise ValueError("project_name contains unsupported characters")
        return value

    @field_validator("compose_file")
    @classmethod
    def validate_compose_file(cls, value: str) -> str:
        if value not in {"compose.yml", "compose.yaml"}:
            raise ValueError("compose_file must be compose.yml or compose.yaml")
        return value

    @field_validator("apps")
    @classmethod
    def validate_app_names(cls, value: dict[str, AppConfig]) -> dict[str, AppConfig]:
        for app_name in value:
            _validate_safe_name(app_name, label="app name")
        return value


class BotmanConfig(StrictModel):
    settings: SettingsConfig = Field(default_factory=SettingsConfig)
    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    stacks: dict[str, StackConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_top_level_apps(cls, raw):
        """Accept v1 flat apps once and normalize them under their owning stack.

        This keeps existing installations bootable after the schema correction.
        Any subsequent save writes only the nested representation.
        """
        if not isinstance(raw, dict) or "apps" not in raw:
            return raw
        data = dict(raw)
        legacy_apps = data.pop("apps")
        if not legacy_apps:
            return data
        if not isinstance(legacy_apps, dict):
            raise ValueError("legacy top-level apps must be a mapping")
        stacks = data.get("stacks")
        if not isinstance(stacks, dict):
            raise ValueError("legacy apps require a stacks mapping")
        stacks = {name: dict(value) if isinstance(value, dict) else value for name, value in stacks.items()}
        for app_name, app_raw in legacy_apps.items():
            if not isinstance(app_raw, dict):
                raise ValueError(f"legacy app {app_name!r} must be a mapping")
            app_data = dict(app_raw)
            stack_name = app_data.pop("stack", None)
            if not isinstance(stack_name, str) or stack_name not in stacks:
                raise ValueError(f"legacy app {app_name!r} references unknown stack {stack_name!r}")
            stack_raw = stacks[stack_name]
            if not isinstance(stack_raw, dict):
                raise ValueError(f"stack {stack_name!r} must be a mapping")
            nested = dict(stack_raw.get("apps") or {})
            if app_name in nested:
                raise ValueError(f"app {app_name!r} is defined twice in stack {stack_name!r}")
            nested[app_name] = app_data
            stack_raw["apps"] = nested
        data["stacks"] = stacks
        return data

    @model_validator(mode="after")
    def validate_relationships(self) -> "BotmanConfig":
        for server_name in self.servers:
            _validate_safe_name(server_name, label="server name")
        channels: dict[str, str] = {}
        projects: dict[tuple[str, str], str] = {}
        per_server_log_ids: set[tuple[str, str]] = set()
        for stack_name, stack in self.stacks.items():
            _validate_safe_name(stack_name, label="stack name")
            if stack.server not in self.servers:
                raise ValueError(
                    f"stack {stack_name!r} references unknown server {stack.server!r}"
                )
            previous = channels.setdefault(stack.channel_id, stack_name)
            if previous != stack_name:
                raise ValueError(
                    f"channel_id {stack.channel_id!r} is assigned to both "
                    f"{previous!r} and {stack_name!r}"
                )

            project_key = (stack.server, stack.project_name)
            previous_project = projects.setdefault(project_key, stack_name)
            if previous_project != stack_name:
                raise ValueError(
                    f"Compose project_name {stack.project_name!r} on server "
                    f"{stack.server!r} is assigned to both {previous_project!r} "
                    f"and {stack_name!r}"
                )

            services: dict[str, str] = {}
            for app_name, app in stack.apps.items():
                _validate_safe_name(app_name, label="app name")
                previous_app = services.setdefault(app.service, app_name)
                if previous_app != app_name:
                    raise ValueError(
                        f"Compose service {app.service!r} in stack {stack_name!r} "
                        f"is assigned to both {previous_app!r} and {app_name!r}"
                    )
                log_key = (stack.server, app.log_identifier)
                if log_key in per_server_log_ids:
                    raise ValueError(
                        f"duplicate log_identifier {app.log_identifier!r} on server "
                        f"{stack.server!r}"
                    )
                per_server_log_ids.add(log_key)
        return self

    def stack_for_channel(self, channel_id: str | int) -> tuple[str, StackConfig]:
        wanted = str(channel_id)
        for stack_name, stack in self.stacks.items():
            if stack.channel_id == wanted:
                return stack_name, stack
        raise KeyError(f"no stack is configured for channel: {wanted}")

    def stack_root(self, stack_name: str) -> PurePosixPath:
        _validate_safe_name(stack_name, label="stack name")
        if stack_name not in self.stacks:
            raise KeyError(f"unknown stack: {stack_name}")
        return STACK_BASE / stack_name

    def compose_path(self, stack_name: str) -> PurePosixPath:
        return self.stack_root(stack_name) / self.stacks[stack_name].compose_file

    def app_release_root(self, stack_name: str, app_name: str) -> PurePosixPath:
        if app_name not in self.stacks[stack_name].apps:
            raise KeyError(f"unknown app {app_name!r} in stack {stack_name!r}")
        return self.stack_root(stack_name) / "apps" / app_name

    def app_env_path(self, stack_name: str, app_name: str) -> PurePosixPath:
        app = self.stacks[stack_name].apps[app_name]
        relative = app.env_file or f"env/{app_name}.env"
        return self.stack_root(stack_name) / PurePosixPath(relative)

    @staticmethod
    def agent_app_key(stack_name: str, app_name: str) -> str:
        _validate_safe_name(stack_name, label="stack name")
        _validate_safe_name(app_name, label="app name")
        return f"{stack_name}.{app_name}"

    def iter_apps(self):
        for stack_name, stack in self.stacks.items():
            for app_name, app in stack.apps.items():
                yield stack_name, app_name, app

    def apps_for_server(self, server_name: str) -> dict[str, AppConfig]:
        if server_name not in self.servers:
            raise KeyError(f"unknown server: {server_name}")
        return {
            self.agent_app_key(stack_name, app_name): app
            for stack_name, stack in self.stacks.items()
            if stack.server == server_name
            for app_name, app in stack.apps.items()
        }

    def apps_for_channel(self, channel_id: str | int) -> dict[str, AppConfig]:
        try:
            _, stack = self.stack_for_channel(channel_id)
        except KeyError:
            return {}
        return dict(stack.apps)
