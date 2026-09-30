"""Admin configuration services used by slash-only Discord commands."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Literal

from .compose import executor_for_server
from .config import ConfigStore
from .executor import ExecResult
from .models import AppConfig, GitConfig, LocalServerConfig, SSHServerConfig, StackConfig


class AdminAuthorizationError(PermissionError):
    pass


class AdminGuard:
    def __init__(self, admin_ids: frozenset[int]):
        self.admin_ids = admin_ids

    def require(self, user_id: int) -> None:
        if user_id not in self.admin_ids:
            raise AdminAuthorizationError("administrator access required")


def parse_compose_argv(value: str) -> tuple[str, ...]:
    try:
        argv = tuple(shlex.split(value))
    except ValueError as exc:
        raise ValueError(f"invalid Compose command: {exc}") from exc
    if not argv:
        raise ValueError("Compose command must not be empty")
    return argv


class AdminConfigService:
    def __init__(self, store: ConfigStore):
        self.store = store

    async def add_server(
        self,
        *,
        name: str,
        server_type: Literal["local", "ssh"],
        compose_argv: tuple[str, ...],
        host: str | None = None,
        port: int = 22,
        user: str | None = None,
        key: str | Path | None = None,
        known_hosts: str | Path | None = None,
    ) -> None:
        def mutate(config):
            if name in config.servers:
                raise ValueError(f"server already exists: {name}")
            if server_type == "local":
                config.servers[name] = LocalServerConfig(
                    type="local",
                    compose_argv=compose_argv,
                )
                return
            missing = [
                field
                for field, value in (("host", host), ("user", user), ("key", key))
                if value in {None, ""}
            ]
            if missing:
                raise ValueError(f"SSH server requires: {', '.join(missing)}")
            config.servers[name] = SSHServerConfig(
                type="ssh",
                host=str(host),
                port=port,
                user=str(user),
                key=Path(str(key)),
                known_hosts=Path(str(known_hosts)) if known_hosts else None,
                compose_argv=compose_argv,
            )

        await self.store.mutate(mutate)

    async def test_server(self, name: str) -> ExecResult:
        config = self.store.load()
        try:
            server = config.servers[name]
        except KeyError as exc:
            raise KeyError(f"unknown server: {name}") from exc
        executor = executor_for_server(server)
        return await executor.run((*server.compose_argv, "version"), timeout=30, check=False)

    async def add_stack(
        self,
        *,
        name: str,
        server: str,
        channel_id: str | int,
        project_name: str | None = None,
    ) -> None:
        def mutate(config):
            if name in config.stacks:
                raise ValueError(f"stack already exists: {name}")
            if server not in config.servers:
                raise ValueError(f"unknown server: {server}")
            derived_project = project_name or (f"botman-{name}" if len(name) <= 56 else name)
            config.stacks[name] = StackConfig(
                server=server,
                channel_id=str(channel_id),
                project_name=derived_project,
            )

        await self.store.mutate(mutate)

    async def add_app(
        self,
        *,
        name: str,
        stack: str,
        service: str,
        repo_url: str,
        branch: str = "main",
        log_identifier: str | None = None,
    ) -> None:
        def mutate(config):
            if name in config.apps:
                raise ValueError(f"app already exists: {name}")
            if stack not in config.stacks:
                raise ValueError(f"unknown stack: {stack}")
            project = config.stacks[stack].project_name
            identifier = log_identifier or f"{project}-{name}"
            config.apps[name] = AppConfig(
                stack=stack,
                service=service,
                log_identifier=identifier,
                git=GitConfig(repo_url=repo_url, branch=branch),
            )

        await self.store.mutate(mutate)
