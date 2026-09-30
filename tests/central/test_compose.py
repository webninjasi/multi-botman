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


class RecordingExecutor:
    def __init__(self, *, delay: float = 0.0, activity: list | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.delay = delay
        self.activity = activity

    async def run(self, argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
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
    app_a = resolve_app(config, "app-a")

    await manager.start(app_a)
    await manager.stop(app_a)
    await manager.restart(app_a)
    await manager.status(app_a)
    await manager.build(app_a)
    await manager.up(app_a)

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
        (*base, "build", "svc-a"),
        (*base, "up", "-d", "--no-deps", "svc-a"),
    ]


@pytest.mark.asyncio
async def test_configured_podman_compose_prefix_is_preserved() -> None:
    config = config_two_apps(ssh=True)
    executor = RecordingExecutor()
    manager = ComposeManager(config, executor_factory=lambda _: executor)
    await manager.start(resolve_app(config, "app-a"))
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
        manager.restart(resolve_app(config, "app-a")),
        manager.restart(resolve_app(config, "app-b")),
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
