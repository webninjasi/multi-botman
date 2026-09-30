"""YAML config loading and serialized atomic mutation."""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import ValidationError

from .models import BotmanConfig


class ConfigError(RuntimeError):
    """Base configuration error."""


class ConfigNotFoundError(ConfigError):
    """Configuration file is missing."""


T = TypeVar("T")
Mutator = Callable[[BotmanConfig], T | Awaitable[T]]


class ConfigStore:
    """Persist Botman YAML safely.

    Mutations are serialized with one asyncio lock and always reload the current
    on-disk state while holding that lock, preventing two interactions from
    overwriting each other with stale copies.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._mutation_lock = asyncio.Lock()

    @property
    def mutation_lock(self) -> asyncio.Lock:
        return self._mutation_lock

    def load(self) -> BotmanConfig:
        if not self.path.exists():
            raise ConfigNotFoundError(f"config file does not exist: {self.path}")
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"failed to read/parse config {self.path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError("top-level config must be a YAML mapping")
        try:
            return BotmanConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"invalid config {self.path}:\n{exc}") from exc

    def load_or_default(self) -> BotmanConfig:
        if not self.path.exists():
            return BotmanConfig()
        return self.load()

    async def save(self, config: BotmanConfig) -> None:
        async with self._mutation_lock:
            validated = BotmanConfig.model_validate(config.model_dump(mode="python"))
            self._save_unlocked(validated)

    async def mutate(self, mutator: Mutator[T]) -> tuple[BotmanConfig, T]:
        async with self._mutation_lock:
            config = self.load_or_default()
            result = mutator(config)
            if inspect.isawaitable(result):
                result = await result
            validated = BotmanConfig.model_validate(config.model_dump(mode="python"))
            self._save_unlocked(validated)
            return validated, result

    def _save_unlocked(self, config: BotmanConfig) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(parent, 0o700)
        except PermissionError:
            # Existing system config directories such as /etc/botman may be
            # root-owned and intentionally not chmod-able by the service user.
            pass

        payload = yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
        )
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=parent)
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600)
            try:
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
