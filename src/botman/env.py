"""Admin-only .env file transport and line-preserving edits."""

from __future__ import annotations

import re
from dataclasses import dataclass

from collections.abc import Callable

from .compose import executor_for_resolved_app
from .models import BotmanConfig
from .routing import resolve_app

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
    return "".join(line for line in content.splitlines(keepends=True) if not _matches_key(line, key))


class EnvService:
    def __init__(self, config: BotmanConfig, *, executor_factory: Callable = executor_for_resolved_app):
        self.config = config
        self.executor_factory = executor_factory

    def _target(self, app_name: str):
        resolved = resolve_app(self.config, app_name)
        return resolved, self.executor_factory(resolved), self.config.app_env_path(app_name)

    async def show(self, app_name: str) -> EnvFile:
        _, executor, path = self._target(app_name)
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

    async def upload(self, app_name: str, data: bytes) -> str:
        if len(data) > ENV_UPLOAD_MAX_BYTES:
            raise EnvError(f"environment upload exceeds {ENV_UPLOAD_MAX_BYTES}-byte limit")
        if b"\x00" in data:
            raise EnvError("environment upload contains NUL bytes")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvError("environment upload is not valid UTF-8") from exc
        _, executor, path = self._target(app_name)
        await executor.run(("mkdir", "-p", "--", str(path.parent)), timeout=30, check=True)
        write_bytes = getattr(executor, "write_bytes", None)
        if not callable(write_bytes):
            raise EnvError("executor does not support file writes")
        await write_bytes(path, data, mode=0o600, atomic=True)
        return str(path)

    async def set(self, app_name: str, key: str, value: str) -> str:
        try:
            existing = (await self.show(app_name)).content
        except EnvMissingError:
            existing = ""
        updated = set_env_value(existing, key, value)
        return await self.upload(app_name, updated.encode())

    async def unset(self, app_name: str, key: str) -> str:
        existing = (await self.show(app_name)).content
        updated = unset_env_value(existing, key)
        return await self.upload(app_name, updated.encode())
