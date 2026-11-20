from __future__ import annotations

import asyncio

import pytest

from botman.compose import ComposeManager, ComposeValidationError, LifecycleService, validate_compose_yaml
from botman.executor import ExecResult
from botman.models import BotmanConfig
from botman.routing import ChannelAuthorizationError, resolve_app


def config_two_apps(*, ssh: bool = False) -> BotmanConfig:
    server = (
        {
            "type": "ssh",
            "host": "10.0.0.2",
            "user": "botmgr",
            "key": "/home/botman/.ssh/server-target",
            "compose_argv": ["sudo", "podman", "compose"],
        }
        if ssh
        else {"type": "local", "compose_argv": ["docker", "compose"]}
    )
    return BotmanConfig.model_validate(
        {
            "servers": {"target": server},
            "stacks": {
                "bots": {
                    "server": "target",
                    "channel_id": "111",
                    "project_name": "botman-bots",
                }
            },
            "apps": {
                "app-a": {
                    "stack": "bots",
                    "service": "svc-a",
                    "log_identifier": "botman-bots-app-a",
                    "git": {"repo_url": "git@github.com:o/a.git", "branch": "main"},
                },
                "app-b": {
                    "stack": "bots",
                    "service": "svc-b",
                    "log_identifier": "botman-bots-app-b",
                    "git": {"repo_url": "git@github.com:o/b.git", "branch": "main"},
                },
            },
        }
    )


INSTALLED_COMPOSE = "services: {svc-a: {}, svc-b: {}, svc-new: {}}\n"


class RecordingExecutor:
    """Records Compose commands; answers the enabled-check `cat` without recording it."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        activity: list | None = None,
        compose: str | None = INSTALLED_COMPOSE,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.delay = delay
        self.activity = activity
        self.compose = compose

    async def run(self, argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        if args[0] == "cat":
            if self.compose is None:
                return ExecResult(args, 1, "", "No such file or directory")
            return ExecResult(args, 0, self.compose, "")
        if args[:2] == ("test", "-e"):
            return ExecResult(args, 0 if self.compose is not None else 1, "", "")
        self.calls.append(args)
        if self.activity is not None:
            self.activity.append(("start", args[-1]))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.activity is not None:
            self.activity.append(("end", args[-1]))
        return ExecResult(args, 0, "ok", "")

    async def stream(self, argv, *, timeout=None, check=False):
        if False:
            yield None


@pytest.mark.asyncio
async def test_compose_argv_is_stack_scoped_and_service_scoped() -> None:
    config = config_two_apps()
    executor = RecordingExecutor()
    manager = ComposeManager(config, executor_factory=lambda _: executor)
    app_a = resolve_app(config, "bots", "app-a")

    await manager.start(app_a)
    await manager.stop(app_a)
    await manager.restart(app_a)
    await manager.status(app_a)
    await manager.build(app_a)
    await manager.up(app_a)
    await manager.apply(app_a)

    base = (
        "docker",
        "compose",
        "-p",
        "botman-bots",
        "-f",
        "/srv/botman/stacks/bots/compose.yml",
    )
    assert executor.calls == [
        (*base, "start", "svc-a"),
        (*base, "stop", "svc-a"),
        (*base, "restart", "svc-a"),
        (*base, "ps", "svc-a"),
        ("test", "-f", "/srv/botman/stacks/bots/env/app-a.env"),
        (*base, "--env-file", "/srv/botman/stacks/bots/env/app-a.env", "build", "svc-a"),
        (*base, "up", "-d", "--no-deps", "svc-a"),
        (*base, "up", "-d", "--no-deps", "svc-a"),
    ]


@pytest.mark.asyncio
async def test_configured_podman_compose_prefix_is_preserved() -> None:
    config = config_two_apps(ssh=True)
    executor = RecordingExecutor()
    manager = ComposeManager(config, executor_factory=lambda _: executor)
    await manager.start(resolve_app(config, "bots", "app-a"))
    assert executor.calls[0][:3] == ("sudo", "podman", "compose")


@pytest.mark.asyncio
async def test_lifecycle_authorizes_before_executor_for_both_adapter_paths() -> None:
    config = config_two_apps()
    executor = RecordingExecutor()
    service = LifecycleService(
        config, ComposeManager(config, executor_factory=lambda _: executor)
    )

    # Slash and prefix adapters will both call this exact method. Exercise it
    # twice to prevent either adapter from needing its own routing semantics.
    await service.execute("start", app_name="app-a", channel_id=111)
    await service.execute("status", app_name="app-a", channel_id="111")
    with pytest.raises(ChannelAuthorizationError):
        await service.execute("restart", app_name="app-a", channel_id="999")
    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_per_stack_lock_serializes_conflicting_app_operations() -> None:
    config = config_two_apps()
    activity = []
    executor = RecordingExecutor(delay=0.03, activity=activity)
    manager = ComposeManager(config, executor_factory=lambda _: executor)

    await asyncio.gather(
        manager.restart(resolve_app(config, "bots", "app-a")),
        manager.restart(resolve_app(config, "bots", "app-b")),
    )
    assert activity in [
        [("start", "svc-a"), ("end", "svc-a"), ("start", "svc-b"), ("end", "svc-b")],
        [("start", "svc-b"), ("end", "svc-b"), ("start", "svc-a"), ("end", "svc-a")],
    ]


def test_compose_validation_accepts_two_apps_sharing_stack() -> None:
    config = config_two_apps()
    content = b"""
services:
  svc-a:
    build:
      context: ./apps/app-a/current
    logging:
      driver: journald
      options:
        tag: botman-bots-app-a
  svc-b:
    build: ./apps/app-b/current
    logging:
      driver: journald
      options:
        tag: botman-bots-app-b
"""
    parsed = validate_compose_yaml(config, "bots", content)
    assert set(parsed["services"]) == {"svc-a", "svc-b"}


@pytest.mark.parametrize(
    "content, match",
    [
        (
            b"services: {svc-a: {build: ./apps/app-a/current}}",
            "journald",
        ),
        (
            b"""
services:
  svc-a:
    build: ../escape
    logging: {driver: journald, options: {tag: botman-bots-app-a}}
  svc-b:
    build: ./apps/app-b/current
    logging: {driver: journald, options: {tag: botman-bots-app-b}}
""",
            "build context",
        ),
    ],
)
def test_compose_validation_rejects_invalid_managed_service_config(content: bytes, match: str) -> None:
    with pytest.raises(ComposeValidationError, match=match):
        validate_compose_yaml(config_two_apps(), "bots", content)

@pytest.mark.asyncio
async def test_store_backed_lifecycle_reloads_config_after_waiting_for_stack_lock(
    tmp_path,
) -> None:
    from botman.compose import StackLockRegistry
    from botman.config import ConfigStore

    cfg = config_two_apps()
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(cfg)
    locks = StackLockRegistry()
    executor = RecordingExecutor()
    service = LifecycleService(store, locks=locks)
    service.compose.executor_factory = lambda _: executor

    lock = await locks.get("bots")
    await lock.acquire()
    task = asyncio.create_task(
        service.execute("restart", app_name="app-a", channel_id=111)
    )
    await asyncio.sleep(0)

    def edit(config):
        config.stacks["bots"].apps["app-a"].service = "svc-new"

    await store.mutate(edit)
    lock.release()
    await task

    assert executor.calls[-1][-2:] == ("restart", "svc-new")


@pytest.mark.asyncio
async def test_store_backed_apply_runs_service_scoped_up(tmp_path) -> None:
    from botman.config import ConfigStore

    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(config_two_apps())
    executor = RecordingExecutor()
    service = LifecycleService(store)
    service.compose.executor_factory = lambda _: executor

    await service.execute("apply", app_name="app-a", channel_id=111)

    assert executor.calls[-1][-4:] == ("up", "-d", "--no-deps", "svc-a")


@pytest.mark.asyncio
async def test_store_backed_rebuild_builds_then_recreates_service(tmp_path) -> None:
    from botman.config import ConfigStore

    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(config_two_apps())
    executor = RecordingExecutor()
    service = LifecycleService(store)
    service.compose.executor_factory = lambda _: executor

    result = await service.execute("rebuild", app_name="app-a", channel_id=111)

    assert executor.calls[-2][-2:] == ("build", "svc-a")
    assert executor.calls[-1][-5:] == ("up", "-d", "--no-deps", "--force-recreate", "svc-a")
    assert result.ok
    assert result.stdout == "[build]\nok\n[up]\nok"


@pytest.mark.asyncio
async def test_rebuild_stops_after_failed_build() -> None:
    config = config_two_apps()

    class FailingBuildExecutor(RecordingExecutor):
        async def run(self, argv, *, timeout=None, check=False):
            result = await super().run(argv, timeout=timeout, check=check)
            if "build" in result.argv:
                return ExecResult(result.argv, 1, "", "boom")
            return result

    executor = FailingBuildExecutor()
    manager = ComposeManager(config, executor_factory=lambda _: executor)

    result = await manager.rebuild(resolve_app(config, "bots", "app-a"))

    assert [call[-2:] for call in executor.calls if call[0] != "test"] == [("build", "svc-a")]
    assert not result.ok
    assert result.stderr == "[build]\nboom"


@pytest.mark.asyncio
async def test_build_skips_env_file_when_app_env_is_missing() -> None:
    config = config_two_apps()

    class NoEnvExecutor(RecordingExecutor):
        async def run(self, argv, *, timeout=None, check=False):
            result = await super().run(argv, timeout=timeout, check=check)
            if result.argv[0] == "test":
                return ExecResult(result.argv, 1, "", "")
            return result

    executor = NoEnvExecutor()
    manager = ComposeManager(config, executor_factory=lambda _: executor)

    await manager.build(resolve_app(config, "bots", "app-a"))

    assert "--env-file" not in executor.calls[-1]
    assert executor.calls[-1][-2:] == ("build", "svc-a")


@pytest.mark.asyncio
async def test_build_only_operation_does_not_recreate_service() -> None:
    config = config_two_apps()
    executor = RecordingExecutor()
    manager = ComposeManager(config, executor_factory=lambda _: executor)

    await manager.execute_unlocked("build", resolve_app(config, "bots", "app-a"))

    assert executor.calls[-1][-2:] == ("build", "svc-a")
    assert not any("up" in call for call in executor.calls)


@pytest.mark.asyncio
async def test_status_falls_back_to_whole_stack_when_ps_rejects_service() -> None:
    class PodmanComposeLikeExecutor(RecordingExecutor):
        async def run(self, argv, *, timeout=None, check=False):
            args = tuple(str(x) for x in argv)
            self.calls.append(args)
            if args[-1] == "svc-a":
                return ExecResult(args, 2, "", "unrecognized arguments: svc-a")
            return ExecResult(args, 0, "svc-a running\nsvc-b running\n", "")

    config = config_two_apps()
    executor = PodmanComposeLikeExecutor()
    manager = ComposeManager(config, executor_factory=lambda _: executor)

    result = await manager.status(resolve_app(config, "bots", "app-a"))

    assert executor.calls[0][-2:] == ("ps", "svc-a")
    assert executor.calls[1][-1] == "ps"
    assert result.ok
    assert result.stdout.startswith("(this Compose provider cannot filter ps by service")


def test_compose_validation_treats_missing_managed_service_as_disabled() -> None:
    content = b"""
services:
  svc-a:
    build: ./apps/app-a/current
    logging: {driver: journald, options: {tag: botman-bots-app-a}}
  db:
    image: postgres:16
"""
    parsed = validate_compose_yaml(config_two_apps(), "bots", content)
    assert set(parsed["services"]) == {"svc-a", "db"}


@pytest.mark.asyncio
@pytest.mark.parametrize("store_backed", [False, True])
async def test_lifecycle_rejects_disabled_app_before_compose(tmp_path, store_backed) -> None:
    from botman.compose import AppDisabledError
    from botman.config import ConfigStore

    cfg = config_two_apps()
    executor = RecordingExecutor(compose="services: {svc-b: {}}\n")
    if store_backed:
        store = ConfigStore(tmp_path / "config.yaml")
        await store.save(cfg)
        service = LifecycleService(store)
        service.compose.executor_factory = lambda _: executor
    else:
        service = LifecycleService(cfg, ComposeManager(cfg, executor_factory=lambda _: executor))

    with pytest.raises(AppDisabledError, match="`app-a` is disabled: service `svc-a`"):
        await service.execute("start", app_name="app-a", channel_id=111)
    await service.execute("start", app_name="app-b", channel_id=111)

    assert executor.calls[-1][-2:] == ("start", "svc-b")
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_lifecycle_without_installed_compose_file_points_to_upload() -> None:
    from botman.compose import AppDisabledError

    cfg = config_two_apps()
    executor = RecordingExecutor(compose=None)
    service = LifecycleService(cfg, ComposeManager(cfg, executor_factory=lambda _: executor))

    with pytest.raises(AppDisabledError, match="no Compose file yet"):
        await service.execute("status", app_name="app-a", channel_id=111)
    assert executor.calls == []


@pytest.mark.parametrize(
    "compose_argv, runtime",
    [
        (("docker", "compose"), ("docker",)),
        (("sudo", "podman", "compose"), ("sudo", "podman")),
        (("podman-compose",), ("podman",)),
        (("/usr/bin/docker-compose",), ("docker",)),
    ],
)
def test_container_runtime_is_derived_from_compose_argv(compose_argv, runtime) -> None:
    from botman.compose import container_runtime_argv

    assert container_runtime_argv(compose_argv) == runtime


def test_container_runtime_derivation_rejects_unknown_command() -> None:
    from botman.compose import container_runtime_argv

    with pytest.raises(ValueError, match="cannot find docker or podman"):
        container_runtime_argv(("nerdctl", "compose"))
