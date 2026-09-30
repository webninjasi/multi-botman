"""Stack-scoped Compose command construction, locking, and config validation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Literal

import yaml

from .executor import ExecResult, Executor, LocalExecutor, SSHExecutor
from .models import BotmanConfig, LocalServerConfig, SSHServerConfig
from .routing import ResolvedApp, authorize_app_channel

MAX_COMPOSE_BYTES = 512 * 1024
LifecycleOperation = Literal["start", "stop", "restart", "status"]


class ComposeValidationError(ValueError):
    """Uploaded Compose YAML does not match the managed stack model."""


class StackLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, stack_name: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(stack_name, asyncio.Lock())


ExecutorFactory = Callable[[ResolvedApp], Executor]


def executor_for_server(server: LocalServerConfig | SSHServerConfig) -> Executor:
    if isinstance(server, LocalServerConfig):
        return LocalExecutor()
    if isinstance(server, SSHServerConfig):
        return SSHExecutor(
            host=server.host,
            port=server.port,
            user=server.user,
            key=server.key,
            known_hosts=server.known_hosts,
            connect_timeout=server.connect_timeout_sec,
        )
    raise TypeError(f"unsupported server config: {type(server)!r}")


def executor_for_resolved_app(resolved: ResolvedApp) -> Executor:
    return executor_for_server(resolved.server)


class ComposeManager:
    def __init__(
        self,
        config: BotmanConfig,
        *,
        executor_factory: ExecutorFactory = executor_for_resolved_app,
        locks: StackLockRegistry | None = None,
    ):
        self.config = config
        self.executor_factory = executor_factory
        self.locks = locks or StackLockRegistry()

    def base_argv(self, resolved: ResolvedApp) -> tuple[str, ...]:
        return (
            *resolved.server.compose_argv,
            "-p",
            resolved.stack.project_name,
            "-f",
            str(self.config.compose_path(resolved.stack_name)),
        )

    async def _run_locked(
        self,
        resolved: ResolvedApp,
        tail: tuple[str, ...],
        *,
        timeout: float | None = None,
    ) -> ExecResult:
        lock = await self.locks.get(resolved.stack_name)
        async with lock:
            return await self.executor_factory(resolved).run(
                (*self.base_argv(resolved), *tail), timeout=timeout
            )

    async def start(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(resolved, ("start", resolved.app.service), timeout=60)

    async def stop(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(resolved, ("stop", resolved.app.service), timeout=60)

    async def restart(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(resolved, ("restart", resolved.app.service), timeout=120)

    async def status(self, resolved: ResolvedApp) -> ExecResult:
        # Use the same lock for a consistent view while another stack mutation
        # is in flight. This is intentionally simple for v1.
        return await self._run_locked(resolved, ("ps", resolved.app.service), timeout=30)

    async def build(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(resolved, ("build", resolved.app.service), timeout=900)

    async def up(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(
            resolved, ("up", "-d", "--no-deps", resolved.app.service), timeout=180
        )


class LifecycleService:
    """Shared authorization boundary for slash and prefix lifecycle commands."""

    def __init__(
        self,
        config: BotmanConfig,
        compose: ComposeManager | None = None,
        *,
        locks: StackLockRegistry | None = None,
    ):
        self.config = config
        self.compose = compose or ComposeManager(config, locks=locks)

    async def execute(
        self,
        operation: LifecycleOperation,
        *,
        app_name: str,
        channel_id: str | int,
    ) -> ExecResult:
        resolved = authorize_app_channel(self.config, app_name, channel_id)
        method = getattr(self.compose, operation)
        return await method(resolved)


def validate_compose_yaml(
    config: BotmanConfig,
    stack_name: str,
    content: bytes | str,
    *,
    max_bytes: int = MAX_COMPOSE_BYTES,
) -> dict:
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw_bytes) > max_bytes:
        raise ComposeValidationError(
            f"Compose attachment is {len(raw_bytes)} bytes; limit is {max_bytes}"
        )
    try:
        parsed = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ComposeValidationError(f"invalid Compose YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ComposeValidationError("Compose document must be a YAML mapping")
    services = parsed.get("services")
    if not isinstance(services, dict):
        raise ComposeValidationError("Compose document must contain a services mapping")
    if stack_name not in config.stacks:
        raise ComposeValidationError(f"unknown stack: {stack_name}")

    for app_name, app in config.apps.items():
        if app.stack != stack_name:
            continue
        service = services.get(app.service)
        if not isinstance(service, dict):
            raise ComposeValidationError(
                f"managed app {app_name!r} requires service {app.service!r}"
            )
        logging = service.get("logging")
        if not isinstance(logging, dict) or logging.get("driver") != "journald":
            raise ComposeValidationError(
                f"service {app.service!r} must use logging.driver: journald"
            )
        options = logging.get("options")
        if not isinstance(options, dict) or str(options.get("tag", "")) != app.log_identifier:
            raise ComposeValidationError(
                f"service {app.service!r} logging.options.tag must be {app.log_identifier!r}"
            )
        build = service.get("build")
        if isinstance(build, str):
            context = build
        elif isinstance(build, dict):
            context = build.get("context")
        else:
            context = None
        expected = PurePosixPath("apps") / app_name / "current"
        if not isinstance(context, str):
            raise ComposeValidationError(
                f"service {app.service!r} must define build context ./{expected}"
            )
        normalized = context[2:] if context.startswith("./") else context
        context_path = PurePosixPath(normalized)
        if context_path.is_absolute() or ".." in context_path.parts or context_path != expected:
            raise ComposeValidationError(
                f"service {app.service!r} build context must be ./{expected}"
            )
    return parsed


class ComposeRuntimeValidationError(RuntimeError):
    """Target Compose runtime rejected a statically valid configuration."""

    def __init__(self, result: ExecResult):
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        super().__init__(f"compose config validation failed: {detail}")


class ComposeTransportError(RuntimeError):
    """Compose file transport or activation failed."""


class ComposeAdminService:
    """Upload/show stack Compose configuration without deploying applications."""

    def __init__(
        self,
        config: BotmanConfig,
        *,
        executor_factory: ExecutorFactory = executor_for_resolved_app,
        locks: StackLockRegistry | None = None,
    ):
        self.config = config
        self.executor_factory = executor_factory
        self.locks = locks or StackLockRegistry()

    def _resolved_for_stack(self, stack_name: str) -> ResolvedApp:
        if stack_name not in self.config.stacks:
            raise KeyError(f"unknown stack: {stack_name}")
        for app_name, app in self.config.apps.items():
            if app.stack == stack_name:
                from .routing import resolve_app

                return resolve_app(self.config, app_name)

        # A stack may be configured before its first app. Build the minimum
        # resolved shape expected by executor factories using a synthetic app
        # is deliberately avoided; use a small stack-aware executor helper.
        stack = self.config.stacks[stack_name]
        server = self.config.servers[stack.server]
        from .models import AppConfig, GitConfig

        placeholder = AppConfig(
            stack=stack_name,
            service="botman-placeholder",
            log_identifier=f"botman-{stack_name}-placeholder",
            git=GitConfig(repo_url="ssh://placeholder.invalid/repo.git"),
        )
        return ResolvedApp(
            name="botman-placeholder",
            app=placeholder,
            stack_name=stack_name,
            stack=stack,
            server_name=stack.server,
            server=server,
        )

    def _base_argv(self, stack_name: str, compose_file: PurePosixPath) -> tuple[str, ...]:
        stack = self.config.stacks[stack_name]
        server = self.config.servers[stack.server]
        return (
            *server.compose_argv,
            "-p",
            stack.project_name,
            "-f",
            str(compose_file),
        )

    async def upload(self, stack_name: str, content: bytes | str) -> ExecResult:
        """Validate, runtime-check, then atomically activate a Compose file."""

        validate_compose_yaml(self.config, stack_name, content)
        payload = content.encode("utf-8") if isinstance(content, str) else content
        resolved = self._resolved_for_stack(stack_name)
        executor = self.executor_factory(resolved)
        stack_root = self.config.stack_root(stack_name)
        target = self.config.compose_path(stack_name)
        import uuid

        staged = stack_root / f".{target.name}.botman-{uuid.uuid4().hex}.tmp"
        lock = await self.locks.get(stack_name)
        async with lock:
            await executor.run(("mkdir", "-p", "--", str(stack_root)), timeout=30, check=True)
            try:
                write_bytes = getattr(executor, "write_bytes", None)
                if not callable(write_bytes):
                    raise ComposeTransportError("executor does not support file writes")
                await write_bytes(staged, payload, mode=0o640, atomic=False)
                result = await executor.run(
                    (*self._base_argv(stack_name, staged), "config"),
                    timeout=60,
                    check=False,
                )
                if not result.ok:
                    raise ComposeRuntimeValidationError(result)
                await executor.run(("mv", "--", str(staged), str(target)), timeout=30, check=True)
                return result
            finally:
                await executor.run(("rm", "-f", "--", str(staged)), timeout=30, check=False)

    async def show(self, stack_name: str) -> str:
        resolved = self._resolved_for_stack(stack_name)
        executor = self.executor_factory(resolved)
        read_bytes = getattr(executor, "read_bytes", None)
        if not callable(read_bytes):
            raise ComposeTransportError("executor does not support file reads")
        data = await read_bytes(self.config.compose_path(stack_name))
        if len(data) > MAX_COMPOSE_BYTES:
            raise ComposeTransportError(
                f"stored Compose file exceeds supported {MAX_COMPOSE_BYTES}-byte limit"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ComposeTransportError("stored Compose file is not valid UTF-8") from exc
