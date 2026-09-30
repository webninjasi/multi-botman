"""Admin-only .env file transport and line-preserving edits."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .compose import StackLockRegistry, executor_for_resolved_app
from .config import ConfigStore
from .models import BotmanConfig
from .routing import ResolvedApp, authorize_app_channel

ENV_UPLOAD_MAX_BYTES = 256 * 1024
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvError(RuntimeError):
    pass


class EnvMissingError(EnvError):
    pass


@dataclass(frozen=True, slots=True)
class EnvFile:
    content: str
    path: str


def _is_missing_exception(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    name = type(exc).__name__.lower()
    return "nosuchfile" in name or "filenotfound" in name


def _matches_key(line: str, key: str) -> bool:
    return bool(re.match(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", line))


def set_env_value(content: str, key: str, value: str) -> str:
    if not ENV_KEY_RE.fullmatch(key):
        raise ValueError("invalid environment variable key")
    if any(ch in value for ch in "\x00\r\n"):
        raise ValueError("environment value must not contain NUL/newlines")
    lines = content.splitlines(keepends=True)
    replacement = f"{key}={value}\n"
    for index, line in enumerate(lines):
        if _matches_key(line, key):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = f"{key}={value}{newline}"
            return "".join(lines)
    if content and not content.endswith(("\n", "\r")):
        return content + "\n" + replacement
    return content + replacement


def unset_env_value(content: str, key: str) -> str:
    if not ENV_KEY_RE.fullmatch(key):
        raise ValueError("invalid environment variable key")
    return "".join(
        line for line in content.splitlines(keepends=True) if not _matches_key(line, key)
    )


class EnvService:
    def __init__(
        self,
        config: BotmanConfig | ConfigStore,
        *,
        executor_factory: Callable = executor_for_resolved_app,
        locks: StackLockRegistry | None = None,
    ):
        self.config_source = config
        self.executor_factory = executor_factory
        self.locks = locks or StackLockRegistry()

    def _config(self) -> BotmanConfig:
        if isinstance(self.config_source, ConfigStore):
            return self.config_source.load_or_default()
        return self.config_source

    def _resolved(self, config: BotmanConfig, app_name: str, channel_id: str | int) -> ResolvedApp:
        return authorize_app_channel(config, app_name, channel_id)

    def _target(self, config: BotmanConfig, resolved: ResolvedApp):
        path = config.app_env_path(resolved.stack_name, resolved.name)
        return self.executor_factory(resolved), path

    async def _read_resolved(self, config: BotmanConfig, resolved: ResolvedApp) -> EnvFile:
        executor, path = self._target(config, resolved)
        read_bytes = getattr(executor, "read_bytes", None)
        if not callable(read_bytes):
            raise EnvError("executor does not support file reads")
        try:
            raw = await read_bytes(path)
        except Exception as exc:
            if _is_missing_exception(exc):
                raise EnvMissingError(f"environment file does not exist: {path}") from exc
            raise EnvError(f"failed to read environment file: {exc}") from exc
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvError("environment file is not valid UTF-8") from exc
        return EnvFile(content, str(path))

    async def _write_resolved(
        self, config: BotmanConfig, resolved: ResolvedApp, data: bytes
    ) -> str:
        executor, path = self._target(config, resolved)
        await executor.run(("mkdir", "-p", "--", str(path.parent)), timeout=30, check=True)
        write_bytes = getattr(executor, "write_bytes", None)
        if not callable(write_bytes):
            raise EnvError("executor does not support file writes")
        await write_bytes(path, data, mode=0o600, atomic=True)
        return str(path)

    @staticmethod
    def _validate_upload(data: bytes) -> None:
        if len(data) > ENV_UPLOAD_MAX_BYTES:
            raise EnvError(f"environment upload exceeds {ENV_UPLOAD_MAX_BYTES}-byte limit")
        if b"\x00" in data:
            raise EnvError("environment upload contains NUL bytes")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvError("environment upload is not valid UTF-8") from exc

    async def show(self, app_name: str, channel_id: str | int) -> EnvFile:
        config = self._config()
        resolved = self._resolved(config, app_name, channel_id)
        return await self._read_resolved(config, resolved)

    async def upload(self, app_name: str, channel_id: str | int, data: bytes) -> str:
        self._validate_upload(data)
        initial = self._resolved(self._config(), app_name, channel_id)
        lock = await self.locks.get(initial.stack_name)
        async with lock:
            config = self._config()
            resolved = self._resolved(config, app_name, channel_id)
            if resolved.stack_name != initial.stack_name:
                raise EnvError("app stack changed while updating environment; retry the command")
            return await self._write_resolved(config, resolved, data)

    async def set(self, app_name: str, channel_id: str | int, key: str, value: str) -> str:
        # Validate caller input before waiting on a potentially long deployment.
        set_env_value("", key, value)
        initial = self._resolved(self._config(), app_name, channel_id)
        lock = await self.locks.get(initial.stack_name)
        async with lock:
            config = self._config()
            resolved = self._resolved(config, app_name, channel_id)
            if resolved.stack_name != initial.stack_name:
                raise EnvError("app stack changed while updating environment; retry the command")
            try:
                existing = (await self._read_resolved(config, resolved)).content
            except EnvMissingError:
                existing = ""
            updated = set_env_value(existing, key, value).encode()
            self._validate_upload(updated)
            return await self._write_resolved(config, resolved, updated)

    async def unset(self, app_name: str, channel_id: str | int, key: str) -> str:
        unset_env_value("", key)
        initial = self._resolved(self._config(), app_name, channel_id)
        lock = await self.locks.get(initial.stack_name)
        async with lock:
            config = self._config()
            resolved = self._resolved(config, app_name, channel_id)
            if resolved.stack_name != initial.stack_name:
                raise EnvError("app stack changed while updating environment; retry the command")
            existing = (await self._read_resolved(config, resolved)).content
            updated = unset_env_value(existing, key).encode()
            return await self._write_resolved(config, resolved, updated)
