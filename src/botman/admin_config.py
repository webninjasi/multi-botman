"""Admin configuration services used by slash-only Discord commands."""

from __future__ import annotations

import shlex
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from .compose import StackLockRegistry, executor_for_server
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
    def __init__(self, store: ConfigStore, *, locks: StackLockRegistry | None = None):
        self.store = store
        self.locks = locks or StackLockRegistry()

    async def _stack_lock_for_channel(self, channel_id: str | int):
        config = self.store.load_or_default()
        try:
            stack_name, _ = config.stack_for_channel(channel_id)
        except KeyError as exc:
            raise ValueError("this channel is not configured for a stack") from exc
        return stack_name, await self.locks.get(stack_name)

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

    async def edit_server(
        self,
        *,
        name: str,
        server_type: Literal["local", "ssh"] | None = None,
        compose_argv: tuple[str, ...] | None = None,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        key: str | Path | None = None,
        known_hosts: str | Path | None = None,
        clear_known_hosts: bool = False,
        connect_timeout_sec: float | None = None,
    ) -> None:
        """Edit an existing server without renaming it.

        Switching between local and SSH is allowed, but switching to SSH requires
        the SSH identity fields which cannot be inferred from a local server.
        ``known_hosts`` can be explicitly cleared so AsyncSSH falls back to the
        account's normal OpenSSH known-hosts handling rather than disabling host
        verification.
        """

        if known_hosts is not None and clear_known_hosts:
            raise ValueError("known_hosts and clear_known_hosts cannot be used together")
        if (
            server_type is None
            and compose_argv is None
            and host is None
            and port is None
            and user is None
            and key is None
            and known_hosts is None
            and not clear_known_hosts
            and connect_timeout_sec is None
        ):
            raise ValueError("provide at least one server field to edit")

        def mutate(config):
            try:
                existing = config.servers[name]
            except KeyError as exc:
                raise ValueError(f"unknown server: {name}") from exc

            target_type = server_type or existing.type
            target_compose = compose_argv or existing.compose_argv

            supplied_ssh_fields = any(
                value is not None
                for value in (host, port, user, key, known_hosts, connect_timeout_sec)
            ) or clear_known_hosts

            if target_type == "local":
                if supplied_ssh_fields:
                    raise ValueError("SSH-only fields cannot be set on a local server")
                config.servers[name] = LocalServerConfig(
                    type="local",
                    compose_argv=target_compose,
                )
                return

            if isinstance(existing, SSHServerConfig):
                target_host = host if host is not None else existing.host
                target_port = port if port is not None else existing.port
                target_user = user if user is not None else existing.user
                target_key = Path(str(key)) if key is not None else existing.key
                if clear_known_hosts:
                    target_known_hosts = None
                elif known_hosts is not None:
                    target_known_hosts = Path(str(known_hosts))
                else:
                    target_known_hosts = existing.known_hosts
                target_timeout = (
                    connect_timeout_sec
                    if connect_timeout_sec is not None
                    else existing.connect_timeout_sec
                )
            else:
                missing = [
                    field
                    for field, value in (("host", host), ("user", user), ("key", key))
                    if value in {None, ""}
                ]
                if missing:
                    raise ValueError(
                        "switching a local server to SSH requires: " + ", ".join(missing)
                    )
                target_host = str(host)
                target_port = port if port is not None else 22
                target_user = str(user)
                target_key = Path(str(key))
                target_known_hosts = Path(str(known_hosts)) if known_hosts else None
                target_timeout = connect_timeout_sec if connect_timeout_sec is not None else 15.0

            config.servers[name] = SSHServerConfig(
                type="ssh",
                host=target_host,
                port=target_port,
                user=target_user,
                key=target_key,
                known_hosts=target_known_hosts,
                connect_timeout_sec=target_timeout,
                compose_argv=target_compose,
            )

        snapshot = self.store.load_or_default()
        stack_names = sorted(
            stack_name
            for stack_name, stack in snapshot.stacks.items()
            if stack.server == name
        )
        async with AsyncExitStack() as held:
            for stack_name in stack_names:
                await held.enter_async_context(await self.locks.get(stack_name))
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

    async def edit_stack(
        self,
        *,
        channel_id: str | int,
        server: str | None = None,
        project_name: str | None = None,
        compose_file: Literal["compose.yml", "compose.yaml"] | None = None,
    ) -> None:
        """Edit the stack bound to ``channel_id``.

        Runtime identity/location changes are intentionally restricted once apps
        exist. Moving an established stack between servers or changing its
        Compose project/file would otherwise orphan deployed files or containers
        without performing the migration that such a change requires.
        """

        if server is None and project_name is None and compose_file is None:
            raise ValueError("provide at least one stack field to edit")

        def mutate(config):
            try:
                stack_name, stack = config.stack_for_channel(channel_id)
            except KeyError as exc:
                raise ValueError("this channel is not configured for a stack") from exc

            identity_changes = (
                (server is not None and server != stack.server)
                or (project_name is not None and project_name != stack.project_name)
                or (compose_file is not None and compose_file != stack.compose_file)
            )
            if stack.apps and identity_changes:
                raise ValueError(
                    f"stack {stack_name} already has apps; server/project/compose identity "
                    "cannot be changed in place because deployed state would need migration"
                )

            if server is not None:
                if server not in config.servers:
                    raise ValueError(f"unknown server: {server}")
                stack.server = server
            if project_name is not None:
                stack.project_name = project_name
            if compose_file is not None:
                stack.compose_file = compose_file

        _, lock = await self._stack_lock_for_channel(channel_id)
        async with lock:
            await self.store.mutate(mutate)

    async def add_app(
        self,
        *,
        name: str,
        channel_id: str | int,
        service: str,
        repo_url: str,
        branch: str = "main",
        log_identifier: str | None = None,
    ) -> None:
        def mutate(config):
            try:
                stack_name, stack = config.stack_for_channel(channel_id)
            except KeyError as exc:
                raise ValueError("this channel is not configured for a stack") from exc
            if name in stack.apps:
                raise ValueError(f"app already exists in stack {stack_name}: {name}")
            identifier = log_identifier or f"{stack.project_name}-{name}"
            stack.apps[name] = AppConfig(
                service=service,
                log_identifier=identifier,
                git=GitConfig(repo_url=repo_url, branch=branch),
            )

        _, lock = await self._stack_lock_for_channel(channel_id)
        async with lock:
            await self.store.mutate(mutate)

    async def edit_app(
        self,
        *,
        name: str,
        channel_id: str | int,
        service: str | None = None,
        repo_url: str | None = None,
        branch: str | None = None,
        log_identifier: str | None = None,
    ) -> None:
        if all(value is None for value in (service, repo_url, branch, log_identifier)):
            raise ValueError("provide at least one app field to edit")

        def mutate(config):
            try:
                stack_name, stack = config.stack_for_channel(channel_id)
            except KeyError as exc:
                raise ValueError("this channel is not configured for a stack") from exc
            try:
                app = stack.apps[name]
            except KeyError as exc:
                raise ValueError(f"unknown app in stack {stack_name}: {name}") from exc

            if log_identifier is not None and log_identifier != app.log_identifier:
                if app.log.live_enabled:
                    raise ValueError(
                        "stop live logs before changing log_identifier so the target agent "
                        "cannot remain subscribed to stale journal metadata"
                    )
                app.log_identifier = log_identifier
            if service is not None:
                app.service = service
            if repo_url is not None:
                app.git.repo_url = repo_url
            if branch is not None:
                app.git.branch = branch

        _, lock = await self._stack_lock_for_channel(channel_id)
        async with lock:
            await self.store.mutate(mutate)
