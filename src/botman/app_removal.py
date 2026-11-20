"""Explicit deletion of a disabled app and everything Botman created for it.

An app is disabled once its service is gone from the stack's Compose file.
Removal then deletes, in an order that is safe to retry after a failure:
its containers and built image, its releases and env file on the target, its
deploy key and Git cache on central, and finally its config entry (followed
by a live-log agent sync when it had live logs). Volumes are never touched.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .agent_control import AgentControlService
from .compose import (
    ExecutorFactory,
    StackLockRegistry,
    container_runtime_argv,
    executor_for_resolved_app,
    parse_compose_yaml,
    project_containers,
    read_installed_compose_text,
)
from .config import ConfigStore
from .executor import ExecResult, Executor
from .live_logs import ServerAgentLockRegistry
from .models import BotmanConfig
from .routing import ResolvedApp, authorize_app_channel

AgentSync = Callable[[BotmanConfig, str], Awaitable[object]]


async def _sync_agent(config: BotmanConfig, server_name: str) -> object:
    return await AgentControlService(config).sync(server_name)


class AppNotDisabledError(RuntimeError):
    """The app's service is still in the Compose file."""


class AppRemovalStaleError(RuntimeError):
    """The app or its containers changed between the preview and confirmation."""


@dataclass(frozen=True, slots=True)
class AppRemovalPlan:
    stack_name: str
    app_name: str
    service: str
    containers: tuple[str, ...]
    images: tuple[str, ...]
    target_paths: tuple[str, ...]
    central_paths: tuple[str, ...]
    live_logs: bool
    webhook_id: str | None
    kept: tuple[str, ...]


class AppRemovalService:
    def __init__(
        self,
        store: ConfigStore,
        *,
        executor_factory: ExecutorFactory = executor_for_resolved_app,
        locks: StackLockRegistry | None = None,
        agent_locks: ServerAgentLockRegistry | None = None,
        agent_sync: AgentSync = _sync_agent,
    ):
        self.store = store
        self.executor_factory = executor_factory
        self.locks = locks or StackLockRegistry()
        self.agent_locks = agent_locks or ServerAgentLockRegistry()
        self.agent_sync = agent_sync

    async def plan(self, *, app_name: str, channel_id: str | int) -> AppRemovalPlan:
        config = self.store.load_or_default()
        resolved = authorize_app_channel(config, app_name, channel_id)
        return await self._plan(self.executor_factory(resolved), config, resolved)

    async def remove(
        self, *, app_name: str, channel_id: str | int, expected: AppRemovalPlan
    ) -> ExecResult:
        """Run a previewed removal. Stops at the first failed step so it can be retried."""

        initial = authorize_app_channel(self.store.load_or_default(), app_name, channel_id)
        lock = await self.locks.get(initial.stack_name)
        async with lock:
            config = self.store.load_or_default()
            resolved = authorize_app_channel(config, app_name, channel_id)
            executor = self.executor_factory(resolved)
            plan = await self._plan(executor, config, resolved)
            if plan != expected:
                raise AppRemovalStaleError(
                    f"`{app_name}` changed since the preview; run `/config app remove` again"
                )
            log: list[str] = []
            runtime = container_runtime_argv(resolved.server.compose_argv)

            if plan.containers:
                removed = await executor.run((*runtime, "rm", "-f", *plan.containers), timeout=120)
                if not self._record(log, "remove containers", removed):
                    return self._result(log, ok=False)
            for image in plan.images:
                # Providers name built images differently, so a missing name is expected.
                result = await executor.run((*runtime, "rmi", image), timeout=120)
                log.append(f"[remove image {image}] " + ("removed" if result.ok else "not present"))
            removed = await executor.run(("rm", "-rf", "--", *plan.target_paths), timeout=120)
            if not self._record(log, "remove target files", removed):
                return self._result(log, ok=False)
            try:
                await asyncio.to_thread(self._remove_central, plan.central_paths)
            except OSError as exc:
                log.append(f"[remove central files] failed: {exc}")
                return self._result(log, ok=False)
            if plan.central_paths:
                log.append("[remove central files] " + ", ".join(plan.central_paths))

            async with self.agent_locks.get(resolved.server_name):

                def forget(config: BotmanConfig) -> None:
                    config.stacks[resolved.stack_name].apps.pop(resolved.name, None)

                updated, _ = await self.store.mutate(forget)
                log.append(f"[config] removed `{resolved.name}` from stack `{resolved.stack_name}`")
                if plan.live_logs:
                    try:
                        await self.agent_sync(updated, resolved.server_name)
                        log.append("[live logs] agent config synchronized")
                    except Exception as exc:
                        log.append(
                            f"[live logs] agent sync failed ({type(exc).__name__}); "
                            f"run `/config agent sync {resolved.server_name}`"
                        )
            return self._result(log, ok=True)

    async def _plan(
        self, executor: Executor, config: BotmanConfig, resolved: ResolvedApp
    ) -> AppRemovalPlan:
        stack_name, app_name, app = resolved.stack_name, resolved.name, resolved.app
        text = await read_installed_compose_text(executor, config, stack_name)
        if text is not None and app.service in parse_compose_yaml(text)["services"]:
            raise AppNotDisabledError(
                f"`{app_name}` is still enabled: remove service `{app.service}` from "
                f"`{resolved.stack.compose_file}` and run `/config compose apply` first."
            )

        containers = await project_containers(executor, config, stack_name)
        project = resolved.stack.project_name
        kept: list[str] = []

        target_paths = [str(config.app_release_root(stack_name, app_name))]
        env_path = config.app_env_path(stack_name, app_name)
        sharing = [
            other
            for other in resolved.stack.apps
            if other != app_name and config.app_env_path(stack_name, other) == env_path
        ]
        if sharing:
            kept.append(f"env file `{env_path}` (also used by {', '.join(sharing)})")
        else:
            target_paths.append(str(env_path))

        settings = config.settings
        central = [settings.repo_cache_root / stack_name / f"{app_name}.git"]
        key = app.git.deploy_key_path
        if key is not None:
            if key == settings.deploy_key_root / stack_name / app_name:
                central += [key, Path(f"{key}.pub")]
            else:
                kept.append(f"custom deploy key `{key}`")

        return AppRemovalPlan(
            stack_name=stack_name,
            app_name=app_name,
            service=app.service,
            containers=tuple(cid for cid, _ in containers.get(app.service, [])),
            # Docker Compose names built images `<project>-<service>`, podman-compose
            # `<project>_<service>`.
            images=(f"{project}-{app.service}", f"{project}_{app.service}"),
            target_paths=tuple(target_paths),
            central_paths=tuple(str(path) for path in central if path.exists()),
            live_logs=app.log.live_enabled,
            webhook_id=app.log.webhook_id,
            kept=tuple(kept),
        )

    @staticmethod
    def _remove_central(paths: tuple[str, ...]) -> None:
        for raw in paths:
            path = Path(raw)
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    @staticmethod
    def _record(log: list[str], label: str, result: ExecResult) -> bool:
        detail = (result.stderr.strip() or result.stdout.strip()) if not result.ok else ""
        log.append(f"[{label}] " + ("ok" if result.ok else f"failed: {detail or result.returncode}"))
        return result.ok

    @staticmethod
    def _result(log: list[str], *, ok: bool) -> ExecResult:
        return ExecResult((), 0 if ok else 1, "\n".join(log), "")
