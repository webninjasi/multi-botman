"""Stack-scoped Compose command construction, locking, and config validation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

import yaml

from .config import ConfigStore
from .executor import ExecResult, Executor, LocalExecutor, SSHExecutor
from .models import BotmanConfig, LocalServerConfig, SSHServerConfig
from .routing import ResolvedApp, authorize_app_channel

MAX_COMPOSE_BYTES = 512 * 1024
LifecycleOperation = Literal["start", "stop", "restart", "status", "apply", "rebuild", "build"]


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

    async def build_argv(
        self, resolved: ResolvedApp, executor: Executor
    ) -> tuple[str, ...]:
        """`compose build` argv that interpolates `${VAR}` from the app env file.

        Compose `env_file:` only reaches the running container, so build args
        would otherwise not see values managed with `/env`. `--env-file` is
        passed only to builds (not `up`) and only when the file exists, because
        Compose rejects a missing `--env-file`.
        """

        env_path = str(self.config.app_env_path(resolved.stack_name, resolved.name))
        probe = await executor.run(("test", "-f", env_path), timeout=30, check=False)
        env_args = ("--env-file", env_path) if probe.ok else ()
        return (*self.base_argv(resolved), *env_args, "build", resolved.app.service)

    def recreate_argv(self, resolved: ResolvedApp) -> tuple[str, ...]:
        """`up` after a build: always recreate so the new image is used.

        podman-compose recreates only on a Compose config change, not on a new
        image, so a plain `up -d` would keep running the old build.
        """

        return (
            *self.base_argv(resolved),
            "up", "-d", "--no-deps", "--force-recreate", resolved.app.service,
        )

    async def _status_unlocked(self, resolved: ResolvedApp) -> ExecResult:
        """Service-scoped `ps`, falling back to the whole stack.

        podman-compose's `ps` takes no service arguments. The fallback only
        shows services of this stack, which the channel already authorizes.
        """

        executor = self.executor_factory(resolved)
        scoped = await executor.run(
            (*self.base_argv(resolved), "ps", resolved.app.service), timeout=30
        )
        if scoped.ok:
            return scoped
        whole = await executor.run((*self.base_argv(resolved), "ps"), timeout=30)
        if not whole.ok:
            return scoped
        note = "(this Compose provider cannot filter ps by service; showing the whole stack)\n"
        return ExecResult(whole.argv, whole.returncode, note + whole.stdout, whole.stderr)

    async def _build_unlocked(self, resolved: ResolvedApp, *, timeout: float) -> ExecResult:
        executor = self.executor_factory(resolved)
        return await executor.run(await self.build_argv(resolved, executor), timeout=timeout)

    async def _run_unlocked(
        self,
        resolved: ResolvedApp,
        tail: tuple[str, ...],
        *,
        timeout: float | None = None,
    ) -> ExecResult:
        return await self.executor_factory(resolved).run(
            (*self.base_argv(resolved), *tail), timeout=timeout
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
            return await self._run_unlocked(resolved, tail, timeout=timeout)

    async def execute_unlocked(
        self, operation: LifecycleOperation, resolved: ResolvedApp
    ) -> ExecResult:
        if operation == "rebuild":
            return await self._rebuild_unlocked(resolved)
        if operation == "status":
            return await self._status_unlocked(resolved)
        if operation == "build":
            # `/rebuild APP build_only`: the running container keeps its old
            # image until the next recreate. Fits the 15-minute reply window.
            return await self._build_unlocked(resolved, timeout=840)
        tails: dict[LifecycleOperation, tuple[tuple[str, ...], float]] = {
            "start": (("start", resolved.app.service), 60),
            "stop": (("stop", resolved.app.service), 60),
            "restart": (("restart", resolved.app.service), 120),
            # `up` recreates the container only when its Compose/env config
            # changed, so this applies edits that `restart` would not pick up.
            "apply": (("up", "-d", "--no-deps", resolved.app.service), 180),
        }
        tail, timeout = tails[operation]
        return await self._run_unlocked(resolved, tail, timeout=timeout)

    async def start(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(resolved, ("start", resolved.app.service), timeout=60)

    async def stop(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(resolved, ("stop", resolved.app.service), timeout=60)

    async def restart(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(resolved, ("restart", resolved.app.service), timeout=120)

    async def status(self, resolved: ResolvedApp) -> ExecResult:
        # Use the same lock for a consistent view while another stack mutation
        # is in flight. This is intentionally simple for v1.
        lock = await self.locks.get(resolved.stack_name)
        async with lock:
            return await self._status_unlocked(resolved)

    async def build(self, resolved: ResolvedApp) -> ExecResult:
        lock = await self.locks.get(resolved.stack_name)
        async with lock:
            return await self._build_unlocked(resolved, timeout=900)

    async def up(self, resolved: ResolvedApp) -> ExecResult:
        return await self._run_locked(
            resolved, ("up", "-d", "--no-deps", resolved.app.service), timeout=180
        )

    async def apply(self, resolved: ResolvedApp) -> ExecResult:
        return await self.up(resolved)

    async def rebuild(self, resolved: ResolvedApp) -> ExecResult:
        lock = await self.locks.get(resolved.stack_name)
        async with lock:
            return await self._rebuild_unlocked(resolved)

    async def _rebuild_unlocked(self, resolved: ResolvedApp) -> ExecResult:
        """Rebuild the active release's image, then recreate the service.

        Equivalent to `up -d --build --no-deps <service>`, split in two so only
        the build gets `--env-file` (see `build_argv`). For apps whose build
        reads env/Compose values (build args), which `apply` cannot pick up
        because it reuses the existing image. Stops at a failed build so the
        running container is left untouched.
        """

        # build + up must finish inside Discord's 15-minute deferred-reply
        # window, so this is shorter than the 900s `/update` build.
        build = await self._build_unlocked(resolved, timeout=660)
        if not build.ok:
            return _combine_steps(("build", build))
        up = await self.executor_factory(resolved).run(
            self.recreate_argv(resolved), timeout=180
        )
        return _combine_steps(("build", build), ("up", up))


def _combine_steps(*steps: tuple[str, ExecResult]) -> ExecResult:
    """Merge multi-step output into one result; the last step decides status."""

    def joined(stream: str) -> str:
        parts = []
        for label, result in steps:
            text = getattr(result, stream).strip()
            if text:
                parts.append(f"[{label}]\n{text}")
        return "\n".join(parts)

    last = steps[-1][1]
    return ExecResult(last.argv, last.returncode, joined("stdout"), joined("stderr"))


class LifecycleService:
    """Shared authorization boundary for slash and prefix lifecycle commands."""

    def __init__(
        self,
        config: BotmanConfig | ConfigStore,
        compose: ComposeManager | None = None,
        *,
        locks: StackLockRegistry | None = None,
    ):
        self.config_source = config
        initial = self._config()
        self.compose = compose or ComposeManager(initial, locks=locks)

    def _config(self) -> BotmanConfig:
        if isinstance(self.config_source, ConfigStore):
            return self.config_source.load_or_default()
        return self.config_source

    async def execute(
        self,
        operation: LifecycleOperation,
        *,
        app_name: str,
        channel_id: str | int,
    ) -> ExecResult:
        if not isinstance(self.config_source, ConfigStore):
            resolved = authorize_app_channel(self.config_source, app_name, channel_id)
            await require_enabled(
                self.compose.executor_factory(resolved), self.config_source, resolved
            )
            method = getattr(self.compose, operation)
            return await method(resolved)

        initial = authorize_app_channel(self._config(), app_name, channel_id)
        lock = await self.compose.locks.get(initial.stack_name)
        async with lock:
            config = self._config()
            resolved = authorize_app_channel(config, app_name, channel_id)
            if resolved.stack_name != initial.stack_name:
                raise RuntimeError("app stack changed while waiting for lifecycle lock; retry")
            await require_enabled(self.compose.executor_factory(resolved), config, resolved)
            fresh = ComposeManager(
                config,
                executor_factory=self.compose.executor_factory,
                locks=self.compose.locks,
            )
            return await fresh.execute_unlocked(operation, resolved)


def parse_compose_yaml(content: bytes | str, *, max_bytes: int = MAX_COMPOSE_BYTES) -> dict:
    """Safely parse a Compose document that has a `services` mapping."""

    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw_bytes) > max_bytes:
        raise ComposeValidationError(
            f"Compose file is {len(raw_bytes)} bytes; limit is {max_bytes}"
        )
    try:
        parsed = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ComposeValidationError(f"invalid Compose YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ComposeValidationError("Compose document must be a YAML mapping")
    if not isinstance(parsed.get("services"), dict):
        raise ComposeValidationError("Compose document must contain a services mapping")
    return parsed


def validate_compose_yaml(
    config: BotmanConfig,
    stack_name: str,
    content: bytes | str,
    *,
    max_bytes: int = MAX_COMPOSE_BYTES,
) -> dict:
    """Validate the services of managed apps that the Compose file defines.

    The Compose file is the source of truth for what runs: a managed app whose
    service is absent is disabled, not an error. Its config is kept until the
    app is deleted explicitly.
    """

    parsed = parse_compose_yaml(content, max_bytes=max_bytes)
    services = parsed["services"]
    if stack_name not in config.stacks:
        raise ComposeValidationError(f"unknown stack: {stack_name}")

    for app_name, app in config.stacks[stack_name].apps.items():
        if app.service not in services:
            continue
        service = services[app.service]
        if not isinstance(service, dict):
            raise ComposeValidationError(f"service {app.service!r} must be a mapping")
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
        context: object
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


class AppDisabledError(RuntimeError):
    """The app's service is not in the installed Compose file (or there is none)."""


class ComposeApplyStaleError(RuntimeError):
    """The Compose file or stack changed between the apply preview and confirmation."""


async def read_installed_compose_text(
    executor: Executor, config: BotmanConfig, stack_name: str
) -> str | None:
    """Return the installed Compose file's text, or None when there is none.

    `cat` keeps the common path to one remote command; `test -e` only runs to
    tell a missing file from an unreadable one.
    """

    path = str(config.compose_path(stack_name))
    result = await executor.run(("cat", "--", path), timeout=30, check=False)
    if result.ok:
        return result.stdout
    probe = await executor.run(("test", "-e", path), timeout=30, check=False)
    if not probe.ok:
        return None
    detail = result.stderr.strip() or f"exit {result.returncode}"
    raise ComposeTransportError(f"cannot read {path}: {detail}")


async def require_enabled(executor: Executor, config: BotmanConfig, resolved: ResolvedApp) -> None:
    """Reject commands for an app whose service is absent from the Compose file."""

    text = await read_installed_compose_text(executor, config, resolved.stack_name)
    if text is None:
        raise AppDisabledError(
            f"Stack `{resolved.stack_name}` has no Compose file yet; "
            "upload one with `/config compose upload`."
        )
    services = parse_compose_yaml(text)["services"]
    if resolved.app.service not in services:
        raise AppDisabledError(
            f"`{resolved.name}` is disabled: service `{resolved.app.service}` is not in "
            f"`{resolved.stack.compose_file}`. Add it back and run `/config compose apply` "
            "to enable it."
        )


def container_runtime_argv(compose_argv: tuple[str, ...]) -> tuple[str, ...]:
    """Derive the container CLI (`docker`/`podman`) from a configured Compose command.

    Keeps any prefix such as `sudo`, so `sudo podman compose` gives `sudo podman`
    and `podman-compose` gives `podman`.
    """

    for index, part in enumerate(compose_argv):
        name = PurePosixPath(part).name
        for runtime in ("docker", "podman"):
            if name in (runtime, f"{runtime}-compose"):
                return (*compose_argv[:index], runtime)
    raise ValueError(
        f"cannot find docker or podman in the Compose command {' '.join(compose_argv)!r}"
    )


async def project_containers(
    executor: Executor, config: BotmanConfig, stack_name: str
) -> dict[str, list[tuple[str, str]]]:
    """(container ID, config hash) of the stack's Compose project, keyed by service label.

    Uses the container runtime rather than `compose ps`, which only knows the
    services still in the file and cannot filter by service on podman-compose.
    """

    stack = config.stacks[stack_name]
    runtime = container_runtime_argv(config.servers[stack.server].compose_argv)
    listed = await executor.run(
        (
            *runtime, "ps", "-a", "-q", "--no-trunc",
            "--filter", f"label=com.docker.compose.project={stack.project_name}",
        ),
        timeout=60,
        check=True,
    )
    ids = listed.stdout.split()
    if not ids:
        return {}
    labels = (
        '{{index .Config.Labels "com.docker.compose.service"}} '
        '{{index .Config.Labels "com.docker.compose.config-hash"}}'
    )
    inspected = await executor.run(
        (*runtime, "inspect", "--format", labels, *ids), timeout=60, check=True
    )
    containers: dict[str, list[tuple[str, str]]] = {}
    for container_id, line in zip(ids, inspected.stdout.splitlines()):
        service, _, config_hash = line.strip().partition(" ")
        if service and service != "<no value>":
            containers.setdefault(service, []).append((container_id, config_hash.strip()))
    return containers


def _depends_on(spec: object) -> tuple[str, ...]:
    if not isinstance(spec, dict):
        return ()
    deps = spec.get("depends_on")
    if isinstance(deps, dict):
        return tuple(str(name) for name in deps)
    if isinstance(deps, list):
        return tuple(str(name) for name in deps)
    return ()


@dataclass(frozen=True, slots=True)
class ComposeApplyPlan:
    """What `/config compose apply` would do, shown before it runs."""

    stack_name: str
    digest: str
    services: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    orphans: tuple[str, ...]
    # Services whose containers already carry the file's config hash. None when
    # the provider cannot report hashes (podman-compose), so `up` decides alone.
    unchanged: tuple[str, ...] | None = None

    @property
    def changed(self) -> tuple[str, ...]:
        unchanged = self.unchanged or ()
        return tuple(service for service in self.services if service not in unchanged)

    @property
    def empty(self) -> bool:
        return not self.services and not self.orphans


@dataclass(frozen=True, slots=True)
class ComposePrunePlan:
    """Project volumes/networks the Compose file no longer declares."""

    stack_name: str
    digest: str
    networks: tuple[str, ...]
    volumes: tuple[str, ...]
    include_volumes: bool

    @property
    def empty(self) -> bool:
        return not self.networks and not (self.include_volumes and self.volumes)


def declared_resource_names(project: str, compose: dict, kind: str) -> set[str]:
    """Runtime names of the top-level `volumes`/`networks` a Compose file declares.

    Compose names them `<project>_<key>` unless the entry sets `name:`. The
    implicit `default` network always counts as declared.
    """

    entries = compose.get(kind)
    declared = dict(entries) if isinstance(entries, dict) else {}
    if kind == "networks":
        declared.setdefault("default", None)
    names = set()
    for key, spec in declared.items():
        custom = spec.get("name") if isinstance(spec, dict) else None
        names.add(str(custom) if custom else f"{project}_{key}")
    return names


class ComposeAdminService:
    """Upload/show stack Compose configuration without deploying applications."""

    def __init__(
        self,
        config: BotmanConfig | ConfigStore,
        *,
        executor_factory: ExecutorFactory = executor_for_resolved_app,
        locks: StackLockRegistry | None = None,
    ):
        self.config_source = config
        self.executor_factory = executor_factory
        self.locks = locks or StackLockRegistry()

    def _config(self) -> BotmanConfig:
        if isinstance(self.config_source, ConfigStore):
            return self.config_source.load_or_default()
        return self.config_source

    def _resolved_for_stack(self, config: BotmanConfig, stack_name: str) -> ResolvedApp:
        if stack_name not in config.stacks:
            raise KeyError(f"unknown stack: {stack_name}")
        for app_name in config.stacks[stack_name].apps:
            from .routing import resolve_app

            return resolve_app(config, stack_name, app_name)

        # A stack may be configured before its first app. Build the minimum
        # resolved shape expected by executor factories using a synthetic app
        # is deliberately avoided; use a small stack-aware executor helper.
        stack = config.stacks[stack_name]
        server = config.servers[stack.server]
        from .models import AppConfig, GitConfig

        placeholder = AppConfig(
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

    @staticmethod
    def _base_argv(
        config: BotmanConfig, stack_name: str, compose_file: PurePosixPath
    ) -> tuple[str, ...]:
        stack = config.stacks[stack_name]
        server = config.servers[stack.server]
        return (
            *server.compose_argv,
            "-p",
            stack.project_name,
            "-f",
            str(compose_file),
        )

    async def upload(self, stack_name: str, content: bytes | str) -> ExecResult:
        """Validate, runtime-check, then atomically activate a Compose file."""

        payload = content.encode("utf-8") if isinstance(content, str) else content
        # Immutable config objects can fail fast. Store-backed commands wait
        # first so a concurrent admin edit cannot make this pre-check stale.
        if not isinstance(self.config_source, ConfigStore):
            validate_compose_yaml(self.config_source, stack_name, payload)
        lock = await self.locks.get(stack_name)
        async with lock:
            config = self._config()
            validate_compose_yaml(config, stack_name, payload)
            resolved = self._resolved_for_stack(config, stack_name)
            executor = self.executor_factory(resolved)
            stack_root = config.stack_root(stack_name)
            target = config.compose_path(stack_name)
            import uuid

            staged = stack_root / f".{target.name}.botman-{uuid.uuid4().hex}.tmp"
            await executor.run(("mkdir", "-p", "--", str(stack_root)), timeout=30, check=True)
            try:
                write_bytes = getattr(executor, "write_bytes", None)
                if not callable(write_bytes):
                    raise ComposeTransportError("executor does not support file writes")
                await write_bytes(staged, payload, mode=0o640, atomic=False)
                result = await executor.run(
                    (*self._base_argv(config, stack_name, staged), "config"),
                    timeout=60,
                    check=False,
                )
                if not result.ok:
                    raise ComposeRuntimeValidationError(result)
                # Re-write the validated payload through the executor's atomic
                # replacement primitive. This avoids shell/coreutils-specific
                # rename semantics and guarantees an existing active file is
                # left untouched if final activation fails.
                await write_bytes(target, payload, mode=0o640, atomic=True)
                return result
            finally:
                await executor.run(("rm", "-f", "--", str(staged)), timeout=30, check=False)

    async def plan_apply(self, stack_name: str) -> ComposeApplyPlan:
        """Preview a state-changing `/config compose apply` without running it."""

        config = self._config()
        executor = self.executor_factory(self._resolved_for_stack(config, stack_name))
        plan, _ = await self._plan(executor, config, stack_name)
        return plan

    async def apply(self, stack_name: str, expected: ComposeApplyPlan) -> ExecResult:
        """Run a previewed plan: remove orphaned containers, then `up -d` the rest.

        The plan is recomputed under the stack lock and must match the preview,
        so a Compose upload, deployment, or container change in between cannot
        make this act on something the operator did not confirm.
        """

        lock = await self.locks.get(stack_name)
        async with lock:
            config = self._config()
            executor = self.executor_factory(self._resolved_for_stack(config, stack_name))
            plan, orphan_ids = await self._plan(executor, config, stack_name)
            if plan != expected:
                raise ComposeApplyStaleError(
                    "the Compose file or the stack changed since the preview; "
                    "run `/config compose apply` again"
                )
            server = config.servers[config.stacks[stack_name].server]
            steps: list[tuple[str, ExecResult]] = []
            if plan.orphans:
                ids = [cid for service in plan.orphans for cid in orphan_ids[service]]
                runtime = container_runtime_argv(server.compose_argv)
                removed = await executor.run((*runtime, "rm", "-f", *ids), timeout=120)
                steps.append(("remove orphans", removed))
                if not removed.ok:
                    return _combine_steps(*steps)
            # `up` builds a service only when its image is missing, which the
            # plan rules out for managed apps by skipping undeployed ones.
            base = self._base_argv(config, stack_name, config.compose_path(stack_name))
            if plan.unchanged is None:
                if plan.services:
                    up = await executor.run((*base, "up", "-d", *plan.services), timeout=780)
                    steps.append(("up", up))
            else:
                # docker-compose on Podman recreates every container on `up`,
                # even unchanged ones, so Botman decides from the config hashes.
                # Unchanged services go first so stopped dependencies are
                # running before changed services start; `--no-recreate` also
                # covers the dependencies `up` pulls in.
                if plan.unchanged:
                    kept = await executor.run(
                        (*base, "up", "-d", "--no-recreate", *plan.unchanged), timeout=300
                    )
                    steps.append(("up unchanged", kept))
                    if not kept.ok:
                        return _combine_steps(*steps)
                if plan.changed:
                    up = await executor.run(
                        (*base, "up", "-d", "--no-deps", *plan.changed), timeout=600
                    )
                    steps.append(("up changed", up))
            if not steps:
                return ExecResult((), 0, "nothing to apply", "")
            return _combine_steps(*steps)

    async def _plan(
        self, executor: Executor, config: BotmanConfig, stack_name: str
    ) -> tuple[ComposeApplyPlan, dict[str, list[str]]]:
        text = await read_installed_compose_text(executor, config, stack_name)
        if text is None:
            raise ComposeTransportError(
                f"stack `{stack_name}` has no Compose file; upload one with `/config compose upload`"
            )
        services = parse_compose_yaml(text)["services"]
        stack = config.stacks[stack_name]

        skipped: dict[str, str] = {}
        for service, spec in services.items():
            if isinstance(spec, dict) and spec.get("profiles"):
                skipped[service] = "has `profiles`, which a plain `up` does not start"
        managed = {
            app.service: app_name
            for app_name, app in stack.apps.items()
            if app.service in services
        }
        deployed = await self._deployed_apps(executor, config, stack_name, managed.values())
        for service, app_name in managed.items():
            if app_name not in deployed:
                skipped.setdefault(
                    service, f"`{app_name}` is not deployed yet; run `/update {app_name}`"
                )
        # `up SERVICE` also starts its dependencies, so a service depending on
        # a skipped one would fail or start it anyway.
        changed = True
        while changed:
            changed = False
            for service, spec in services.items():
                if service in skipped:
                    continue
                blocked = next((dep for dep in _depends_on(spec) if dep in skipped), None)
                if blocked is not None:
                    skipped[service] = f"depends on skipped `{blocked}`"
                    changed = True

        containers = await project_containers(executor, config, stack_name)
        orphan_ids = {
            service: [cid for cid, _ in found]
            for service, found in containers.items()
            if service not in services
        }
        planned = tuple(service for service in services if service not in skipped)
        hashes = await self._config_hashes(executor, config, stack_name)
        unchanged = None
        if hashes is not None:
            unchanged = tuple(
                service
                for service in planned
                if service in hashes
                and containers.get(service)
                and all(found_hash == hashes[service] for _, found_hash in containers[service])
            )
        plan = ComposeApplyPlan(
            stack_name=stack_name,
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            services=planned,
            skipped=tuple((service, skipped[service]) for service in services if service in skipped),
            orphans=tuple(sorted(orphan_ids)),
            unchanged=unchanged,
        )
        return plan, orphan_ids

    async def _config_hashes(
        self, executor: Executor, config: BotmanConfig, stack_name: str
    ) -> dict[str, str] | None:
        """Per-service config hashes as `up` would label them, or None if unsupported."""

        base = self._base_argv(config, stack_name, config.compose_path(stack_name))
        result = await executor.run((*base, "config", "--hash", "*"), timeout=60, check=False)
        if not result.ok:
            return None
        hashes = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                hashes[parts[0]] = parts[1]
        return hashes or None

    @staticmethod
    async def _deployed_apps(
        executor: Executor, config: BotmanConfig, stack_name: str, app_names
    ) -> set[str]:
        """Apps whose `current` release resolves to a directory, in one remote call."""

        paths = {
            str(config.app_release_root(stack_name, app_name) / "current"): app_name
            for app_name in app_names
        }
        if not paths:
            return set()
        # `ls` exits non-zero when any path is missing but still lists the rest.
        listed = await executor.run(("ls", "-1dL", "--", *paths), timeout=30, check=False)
        return {paths[line] for line in listed.stdout.splitlines() if line in paths}

    async def plan_prune(self, stack_name: str, *, include_volumes: bool) -> ComposePrunePlan:
        config = self._config()
        executor = self.executor_factory(self._resolved_for_stack(config, stack_name))
        return await self._prune_plan(executor, config, stack_name, include_volumes)

    async def prune(self, stack_name: str, expected: ComposePrunePlan) -> ExecResult:
        """Remove what a previewed prune plan lists, if it still matches.

        The runtime refuses to remove a network or volume a container still
        uses, so a failure there leaves it in place and is reported.
        """

        lock = await self.locks.get(stack_name)
        async with lock:
            config = self._config()
            executor = self.executor_factory(self._resolved_for_stack(config, stack_name))
            plan = await self._prune_plan(executor, config, stack_name, expected.include_volumes)
            if plan != expected:
                raise ComposeApplyStaleError(
                    "the Compose file or the stack changed since the preview; "
                    "run `/config compose prune` again"
                )
            stack = config.stacks[stack_name]
            runtime = container_runtime_argv(config.servers[stack.server].compose_argv)
            steps: list[tuple[str, ExecResult]] = []
            if plan.networks:
                result = await executor.run((*runtime, "network", "rm", *plan.networks), timeout=120)
                steps.append(("remove networks", result))
            if plan.include_volumes and plan.volumes:
                result = await executor.run((*runtime, "volume", "rm", *plan.volumes), timeout=120)
                steps.append(("remove volumes", result))
            if not steps:
                return ExecResult((), 0, "nothing to prune", "")
            # Report failure if any step failed, not just the last one.
            failed = next((result for _, result in steps if not result.ok), None)
            combined = _combine_steps(*steps)
            if failed is not None:
                return ExecResult(combined.argv, failed.returncode, combined.stdout, combined.stderr)
            return combined

    async def _prune_plan(
        self, executor: Executor, config: BotmanConfig, stack_name: str, include_volumes: bool
    ) -> ComposePrunePlan:
        text = await read_installed_compose_text(executor, config, stack_name)
        if text is None:
            raise ComposeTransportError(
                f"stack `{stack_name}` has no Compose file; upload one with `/config compose upload`"
            )
        compose = parse_compose_yaml(text)
        stack = config.stacks[stack_name]
        runtime = container_runtime_argv(config.servers[stack.server].compose_argv)
        found = {}
        for kind, command in (("networks", "network"), ("volumes", "volume")):
            listed = await executor.run(
                (
                    *runtime, command, "ls",
                    "--filter", f"label=com.docker.compose.project={stack.project_name}",
                    "--format", "{{.Name}}",
                ),
                timeout=60,
                check=True,
            )
            declared = declared_resource_names(stack.project_name, compose, kind)
            found[kind] = tuple(
                sorted(name for name in set(listed.stdout.split()) if name not in declared)
            )
        return ComposePrunePlan(
            stack_name=stack_name,
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            networks=found["networks"],
            volumes=found["volumes"],
            include_volumes=include_volumes,
        )

    async def show(self, stack_name: str) -> str:
        config = self._config()
        resolved = self._resolved_for_stack(config, stack_name)
        executor = self.executor_factory(resolved)
        read_bytes = getattr(executor, "read_bytes", None)
        if not callable(read_bytes):
            raise ComposeTransportError("executor does not support file reads")
        data = await read_bytes(config.compose_path(stack_name))
        if len(data) > MAX_COMPOSE_BYTES:
            raise ComposeTransportError(
                f"stored Compose file exceeds supported {MAX_COMPOSE_BYTES}-byte limit"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ComposeTransportError("stored Compose file is not valid UTF-8") from exc
