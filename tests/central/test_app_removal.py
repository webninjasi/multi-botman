from __future__ import annotations

import pytest

from botman.app_removal import (
    AppNotDisabledError,
    AppRemovalService,
    AppRemovalStaleError,
)
from botman.config import ConfigStore
from botman.executor import ExecResult
from botman.models import BotmanConfig

ROOT = "/srv/botman/stacks/bots"


def config(tmp_path, *, app_b: dict | None = None) -> BotmanConfig:
    keys = tmp_path / "keys"
    b = {
        "service": "svc-b",
        "log_identifier": "botman-bots-app-b",
        "git": {
            "repo_url": "git@github.com:o/b.git",
            "deploy_key_path": str(keys / "bots" / "app-b"),
        },
        "log": {
            "webhook_id": "555",
            "webhook_url": "https://discord.example/api/webhooks/555/token",
            "live_enabled": True,
            "thread_id": "777",
            "subscription_id": "sub-1",
        },
    }
    b.update(app_b or {})
    return BotmanConfig.model_validate(
        {
            "settings": {
                "repo_cache_root": str(tmp_path / "repos"),
                "deploy_key_root": str(keys),
            },
            "servers": {"target": {"type": "local", "compose_argv": ["docker", "compose"]}},
            "stacks": {
                "bots": {
                    "server": "target",
                    "channel_id": "111",
                    "project_name": "botman-bots",
                    "apps": {
                        "app-a": {
                            "service": "svc-a",
                            "log_identifier": "botman-bots-app-a",
                            "git": {"repo_url": "git@github.com:o/a.git"},
                        },
                        "app-b": b,
                    },
                }
            },
        }
    )


class TargetExecutor:
    def __init__(self, compose: str, containers: dict[str, str], *, rm_rf_code: int = 0) -> None:
        self.compose = compose
        self.containers = dict(containers)
        self.rm_rf_code = rm_rf_code
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        self.calls.append(args)
        out, rc = "", 0
        if args[0] == "cat":
            out = self.compose
        elif args[:2] == ("docker", "ps"):
            out = "".join(f"{cid}\n" for cid in self.containers)
        elif args[:2] == ("docker", "inspect"):
            out = "".join(f"{self.containers[cid]} hash\n" for cid in args[4:])
        elif args[:3] == ("docker", "rm", "-f"):
            for cid in args[3:]:
                self.containers.pop(cid)
        elif args[:2] == ("docker", "rmi"):
            rc = 0 if args[2] == "botman-bots-svc-b" else 1
        elif args[:2] == ("rm", "-rf"):
            rc = self.rm_rf_code
        result = ExecResult(args, rc, out, "")
        return result.check() if check else result

    def mutations(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == "rm" or call[1:2] in (("rm",), ("rmi",))]


DISABLED_B = "services: {svc-a: {}}\n"


async def setup(tmp_path, executor, **config_overrides):
    cfg = config(tmp_path, **config_overrides)
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(cfg)
    synced: list[tuple[BotmanConfig, str]] = []

    async def agent_sync(updated, server):
        synced.append((updated, server))

    service = AppRemovalService(store, executor_factory=lambda _: executor, agent_sync=agent_sync)
    return store, service, synced


def make_central_files(tmp_path) -> list:
    key = tmp_path / "keys" / "bots" / "app-b"
    key.parent.mkdir(parents=True)
    key.write_text("private")
    (tmp_path / "keys" / "bots" / "app-b.pub").write_text("public")
    cache = tmp_path / "repos" / "bots" / "app-b.git"
    (cache / "objects").mkdir(parents=True)
    return [cache, key, tmp_path / "keys" / "bots" / "app-b.pub"]


@pytest.mark.asyncio
async def test_enabled_app_is_refused_before_anything_is_removed(tmp_path) -> None:
    executor = TargetExecutor("services: {svc-a: {}, svc-b: {}}\n", {"c-b": "svc-b"})
    _, service, _ = await setup(tmp_path, executor)

    with pytest.raises(AppNotDisabledError, match="remove service `svc-b`"):
        await service.plan(app_name="app-b", channel_id=111)
    assert executor.mutations() == []


@pytest.mark.asyncio
async def test_removal_deletes_target_central_and_config_state_in_order(tmp_path) -> None:
    central = make_central_files(tmp_path)
    executor = TargetExecutor(DISABLED_B, {"c-a": "svc-a", "c-b": "svc-b"})
    store, service, synced = await setup(tmp_path, executor)

    plan = await service.plan(app_name="app-b", channel_id=111)
    assert plan.containers == ("c-b",)
    assert plan.central_paths == tuple(str(path) for path in central)
    assert plan.live_logs and plan.webhook_id == "555"

    result = await service.remove(app_name="app-b", channel_id=111, expected=plan)

    assert result.ok, result.stdout
    assert executor.mutations() == [
        ("docker", "rm", "-f", "c-b"),
        ("docker", "rmi", "botman-bots-svc-b"),
        ("docker", "rmi", "botman-bots_svc-b"),
        ("rm", "-rf", "--", f"{ROOT}/apps/app-b", f"{ROOT}/env/app-b.env"),
    ]
    assert not any(path.exists() for path in central)
    assert "app-b" not in store.load().stacks["bots"].apps
    assert "app-a" in store.load().stacks["bots"].apps
    [(updated, server)] = synced
    assert server == "target" and "app-b" not in updated.stacks["bots"].apps
    assert executor.containers == {"c-a": "svc-a"}


@pytest.mark.asyncio
async def test_failed_target_cleanup_keeps_config_for_a_retry(tmp_path) -> None:
    executor = TargetExecutor(DISABLED_B, {}, rm_rf_code=1)
    store, service, synced = await setup(tmp_path, executor)
    plan = await service.plan(app_name="app-b", channel_id=111)

    result = await service.remove(app_name="app-b", channel_id=111, expected=plan)

    assert not result.ok
    assert "app-b" in store.load().stacks["bots"].apps
    assert synced == []


@pytest.mark.asyncio
async def test_removal_refuses_when_state_changed_after_preview(tmp_path) -> None:
    executor = TargetExecutor(DISABLED_B, {})
    _, service, _ = await setup(tmp_path, executor)
    plan = await service.plan(app_name="app-b", channel_id=111)
    executor.containers["c-new"] = "svc-b"

    with pytest.raises(AppRemovalStaleError):
        await service.remove(app_name="app-b", channel_id=111, expected=plan)
    assert executor.mutations() == []


@pytest.mark.asyncio
async def test_shared_env_file_and_custom_deploy_key_are_kept(tmp_path) -> None:
    executor = TargetExecutor(DISABLED_B, {})
    _, service, _ = await setup(
        tmp_path,
        executor,
        app_b={
            "env_file": "env/app-a.env",
            "git": {"repo_url": "git@github.com:o/b.git", "deploy_key_path": "/etc/keys/b"},
            "log": {},
        },
    )

    plan = await service.plan(app_name="app-b", channel_id=111)

    assert plan.target_paths == (f"{ROOT}/apps/app-b",)
    assert plan.kept == (
        f"env file `{ROOT}/env/app-a.env` (also used by app-a)",
        "custom deploy key `/etc/keys/b`",
    )
    assert not plan.live_logs


@pytest.mark.asyncio
async def test_app_without_installed_compose_file_can_be_removed(tmp_path) -> None:
    class NoComposeExecutor(TargetExecutor):
        async def run(self, argv, *, timeout=None, check=False):
            if argv[0] in ("cat", "test"):
                return ExecResult(tuple(argv), 1, "", "No such file or directory")
            return await super().run(argv, timeout=timeout, check=check)

    executor = NoComposeExecutor("", {})
    _, service, _ = await setup(tmp_path, executor)

    plan = await service.plan(app_name="app-b", channel_id=111)

    assert plan.app_name == "app-b"


def test_removal_plan_rendering_lists_deletions_and_kept_items(tmp_path) -> None:
    from botman.app_removal import AppRemovalPlan
    from botman.commands.app_remove import render_removal_plan

    text = render_removal_plan(
        AppRemovalPlan(
            stack_name="bots",
            app_name="app-b",
            service="svc-b",
            containers=("c-b",),
            images=("botman-bots-svc-b", "botman-bots_svc-b"),
            target_paths=(f"{ROOT}/apps/app-b",),
            central_paths=("/var/lib/botman/repos/bots/app-b.git",),
            live_logs=True,
            webhook_id="555",
            kept=("custom deploy key `/etc/keys/b`",),
        )
    )

    assert "cannot be undone" in text
    assert f"`{ROOT}/apps/app-b`" in text
    assert "Kept: custom deploy key `/etc/keys/b`" in text
    assert "Volumes are never deleted." in text
