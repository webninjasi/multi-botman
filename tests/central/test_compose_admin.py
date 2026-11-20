from __future__ import annotations

import pytest

from botman.compose import (
    ComposeAdminService,
    ComposeRuntimeValidationError,
    ComposeValidationError,
)
from botman.executor import ExecResult
from botman.models import BotmanConfig


def config() -> BotmanConfig:
    return BotmanConfig.model_validate(
        {
            "servers": {"target": {"type": "local", "compose_argv": ["docker", "compose"]}},
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
                    "git": {"repo_url": "git@github.com:o/a.git"},
                }
            },
        }
    )


VALID = b"""
services:
  svc-a:
    build: ./apps/app-a/current
    logging:
      driver: journald
      options:
        tag: botman-bots-app-a
"""


class MemoryExecutor:
    def __init__(self, *, config_returncode: int = 0) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, ...]] = []
        self.config_returncode = config_returncode

    async def run(self, argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        self.calls.append(args)
        if args[-1] == "config":
            return ExecResult(args, self.config_returncode, "normalized", "bad compose" if self.config_returncode else "")
        if args[:3] == ("rm", "-f", "--"):
            self.files.pop(args[-1], None)
        result = ExecResult(args, 0, "", "")
        return result.check() if check else result

    async def write_bytes(self, path, data, *, mode=0o600, atomic=True):
        self.files[str(path)] = data

    async def read_bytes(self, path):
        return self.files[str(path)]


@pytest.mark.asyncio
async def test_compose_upload_runtime_validates_stage_then_atomically_activates() -> None:
    cfg = config()
    executor = MemoryExecutor()
    service = ComposeAdminService(cfg, executor_factory=lambda _: executor)

    result = await service.upload("bots", VALID)

    target = "/srv/botman/stacks/bots/compose.yml"
    assert result.ok
    assert executor.files[target] == VALID
    config_calls = [call for call in executor.calls if call[-1] == "config"]
    assert len(config_calls) == 1
    assert config_calls[0][:2] == ("docker", "compose")
    assert config_calls[0][2:6] == ("-p", "botman-bots", "-f", config_calls[0][5])
    assert config_calls[0][5].startswith("/srv/botman/stacks/bots/.compose.yml.botman-")
    assert not any(call[0] == "mv" for call in executor.calls)


@pytest.mark.asyncio
async def test_static_validation_failure_writes_nothing() -> None:
    cfg = config()
    executor = MemoryExecutor()
    service = ComposeAdminService(cfg, executor_factory=lambda _: executor)

    with pytest.raises(ComposeValidationError):
        await service.upload("bots", b"services: {svc-a: {build: ../bad}}")
    assert executor.files == {}
    assert executor.calls == []


@pytest.mark.asyncio
async def test_runtime_validation_failure_never_replaces_active_compose() -> None:
    cfg = config()
    executor = MemoryExecutor(config_returncode=1)
    target = "/srv/botman/stacks/bots/compose.yml"
    executor.files[target] = b"old"
    service = ComposeAdminService(cfg, executor_factory=lambda _: executor)

    with pytest.raises(ComposeRuntimeValidationError, match="bad compose"):
        await service.upload("bots", VALID)

    assert executor.files[target] == b"old"
    assert not any(call[0] == "mv" for call in executor.calls)
    assert not any(".botman-" in path for path in executor.files)


@pytest.mark.asyncio
async def test_compose_show_reads_utf8_content() -> None:
    cfg = config()
    executor = MemoryExecutor()
    target = "/srv/botman/stacks/bots/compose.yml"
    executor.files[target] = VALID
    service = ComposeAdminService(cfg, executor_factory=lambda _: executor)
    assert await service.show("bots") == VALID.decode()


class FinalWriteFailExecutor(MemoryExecutor):
    async def write_bytes(self, path, data, *, mode=0o600, atomic=True):
        path_text = str(path)
        if path_text.endswith("/compose.yml") and atomic:
            raise OSError("activation write failed")
        await super().write_bytes(path, data, mode=mode, atomic=atomic)


@pytest.mark.asyncio
async def test_final_compose_activation_failure_preserves_existing_file() -> None:
    cfg = config()
    executor = FinalWriteFailExecutor()
    target = "/srv/botman/stacks/bots/compose.yml"
    executor.files[target] = b"old"
    service = ComposeAdminService(cfg, executor_factory=lambda _: executor)

    with pytest.raises(OSError, match="activation write failed"):
        await service.upload("bots", VALID)

    assert executor.files[target] == b"old"
    assert not any(".botman-" in path for path in executor.files)

@pytest.mark.asyncio
async def test_store_backed_compose_upload_revalidates_after_waiting_for_stack_lock(
    tmp_path,
) -> None:
    import asyncio

    from botman.compose import StackLockRegistry
    from botman.config import ConfigStore

    cfg = config()
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(cfg)
    locks = StackLockRegistry()
    executor = MemoryExecutor()
    service = ComposeAdminService(
        store,
        executor_factory=lambda _: executor,
        locks=locks,
    )

    lock = await locks.get("bots")
    await lock.acquire()
    task = asyncio.create_task(service.upload("bots", VALID))
    await asyncio.sleep(0)

    def edit(config):
        config.stacks["bots"].apps["app-a"].log_identifier = "botman-bots-renamed"

    await store.mutate(edit)
    lock.release()

    with pytest.raises(ComposeValidationError, match="botman-bots-renamed"):
        await task
    assert executor.files == {}
    assert executor.calls == []


def two_app_config() -> BotmanConfig:
    return BotmanConfig.model_validate(
        {
            "servers": {"target": {"type": "local", "compose_argv": ["docker", "compose"]}},
            "stacks": {
                "bots": {
                    "server": "target",
                    "channel_id": "111",
                    "project_name": "botman-bots",
                    "apps": {
                        name: {
                            "service": service,
                            "log_identifier": f"botman-bots-{name}",
                            "git": {"repo_url": f"git@github.com:o/{name}.git"},
                        }
                        for name, service in {"app-a": "svc-a", "app-b": "svc-b"}.items()
                    },
                }
            },
        }
    )


STACK_COMPOSE = """\
services:
  svc-a:
    build: ./apps/app-a/current
    logging: {driver: journald, options: {tag: botman-bots-app-a}}
  svc-b:
    build: ./apps/app-b/current
    logging: {driver: journald, options: {tag: botman-bots-app-b}}
  db:
    image: postgres:16
  web:
    image: nginx:1.27
    depends_on: {svc-b: {condition: service_started}}
  tools:
    image: busybox:1.36
    profiles: [debug]
"""

ROOT = "/srv/botman/stacks/bots"


class StackExecutor:
    """Fake target with a Compose file, deployed releases, and labelled containers."""

    def __init__(
        self,
        compose: str,
        *,
        deployed: set[str],
        containers: dict[str, str],
        hashes: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.compose = compose
        self.deployed = {f"{ROOT}/apps/{app}/current" for app in deployed}
        self.containers = dict(containers)
        # `compose config --hash` output; None mimics podman-compose, which lacks it.
        self.hashes = hashes
        # Config-hash label per container ID.
        self.labels = labels or {}
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        self.calls.append(args)
        out, rc = "", 0
        if args[0] == "cat":
            out = self.compose
        elif args[0] == "ls":
            paths = args[args.index("--") + 1 :]
            found = [path for path in paths if path in self.deployed]
            out = "".join(f"{path}\n" for path in found)
            rc = 0 if len(found) == len(paths) else 2
        elif args[:2] == ("docker", "ps"):
            assert "label=com.docker.compose.project=botman-bots" in args
            out = "".join(f"{cid}\n" for cid in self.containers)
        elif args[:2] == ("docker", "inspect"):
            out = "".join(
                f"{self.containers[cid]} {self.labels.get(cid, '<no value>')}\n" for cid in args[4:]
            )
        elif "--hash" in args:
            if self.hashes is None:
                rc = 1
            else:
                out = "".join(f"{service} {value}\n" for service, value in self.hashes.items())
        elif args[:3] == ("docker", "rm", "-f"):
            for cid in args[3:]:
                self.containers.pop(cid)
        result = ExecResult(args, rc, out, "")
        return result.check() if check else result

    def mutations(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if "rm" in call[:2] or "up" in call]

    def ups(self) -> list[tuple[str, ...]]:
        return [call[call.index("up") :] for call in self.calls if "up" in call]


def stack_executor(**overrides) -> StackExecutor:
    options = {
        "deployed": {"app-a"},
        "containers": {"c-a": "svc-a", "c-old1": "old-worker", "c-old2": "old-worker", "c-db": "db"},
    }
    options.update(overrides)
    return StackExecutor(STACK_COMPOSE, **options)


@pytest.mark.asyncio
async def test_apply_plan_skips_undeployed_apps_their_dependents_and_profiles() -> None:
    executor = stack_executor()
    service = ComposeAdminService(two_app_config(), executor_factory=lambda _: executor)

    plan = await service.plan_apply("bots")

    assert plan.services == ("svc-a", "db")
    assert plan.skipped == (
        ("svc-b", "`app-b` is not deployed yet; run `/update app-b`"),
        ("web", "depends on skipped `svc-b`"),
        ("tools", "has `profiles`, which a plain `up` does not start"),
    )
    assert plan.orphans == ("old-worker",)
    assert executor.mutations() == []


@pytest.mark.asyncio
async def test_apply_removes_orphans_then_ups_planned_services() -> None:
    executor = stack_executor()
    service = ComposeAdminService(two_app_config(), executor_factory=lambda _: executor)
    plan = await service.plan_apply("bots")

    result = await service.apply("bots", plan)

    assert result.ok
    assert executor.mutations() == [
        ("docker", "rm", "-f", "c-old1", "c-old2"),
        (
            "docker", "compose", "-p", "botman-bots", "-f", f"{ROOT}/compose.yml",
            "up", "-d", "svc-a", "db",
        ),
    ]


@pytest.mark.asyncio
async def test_apply_refuses_when_compose_changed_after_preview() -> None:
    from botman.compose import ComposeApplyStaleError

    executor = stack_executor()
    service = ComposeAdminService(two_app_config(), executor_factory=lambda _: executor)
    plan = await service.plan_apply("bots")
    executor.compose += "  cache:\n    image: redis:7\n"

    with pytest.raises(ComposeApplyStaleError):
        await service.apply("bots", plan)
    assert executor.mutations() == []


@pytest.mark.asyncio
async def test_apply_with_nothing_running_and_no_orphans_only_runs_up() -> None:
    executor = stack_executor(deployed={"app-a", "app-b"}, containers={})
    service = ComposeAdminService(two_app_config(), executor_factory=lambda _: executor)
    plan = await service.plan_apply("bots")

    assert plan.services == ("svc-a", "svc-b", "db", "web")
    await service.apply("bots", plan)
    assert [call[-6:] for call in executor.mutations()] == [
        ("up", "-d", "svc-a", "svc-b", "db", "web")
    ]


@pytest.mark.asyncio
async def test_upload_accepts_file_without_a_managed_apps_service() -> None:
    executor = MemoryExecutor()
    service = ComposeAdminService(two_app_config(), executor_factory=lambda _: executor)

    await service.upload("bots", b"services: {db: {image: postgres:16}}")

    assert executor.files[f"{ROOT}/compose.yml"] == b"services: {db: {image: postgres:16}}"


def test_apply_plan_rendering_lists_every_action() -> None:
    from botman.commands.compose_apply import render_apply_plan
    from botman.compose import ComposeApplyPlan

    text = render_apply_plan(
        ComposeApplyPlan(
            stack_name="bots",
            digest="x",
            services=("svc-a", "db"),
            skipped=(("svc-b", "not deployed"),),
            orphans=("old-worker",),
        )
    )

    assert "`old-worker`" in text
    assert "`svc-a`, `db`" in text
    assert "Skip `svc-b`: not deployed" in text
    assert "never deleted" in text


@pytest.mark.asyncio
async def test_apply_with_config_hashes_keeps_unchanged_services_from_being_recreated() -> None:
    executor = stack_executor(
        deployed={"app-a", "app-b"},
        containers={"c-a": "svc-a", "c-b": "svc-b", "c-db": "db"},
        hashes={"svc-a": "ha", "svc-b": "hb-new", "db": "hd", "web": "hw", "tools": "ht"},
        labels={"c-a": "ha", "c-b": "hb-old", "c-db": "hd"},
    )
    service = ComposeAdminService(two_app_config(), executor_factory=lambda _: executor)

    plan = await service.plan_apply("bots")

    assert plan.unchanged == ("svc-a", "db")
    assert plan.changed == ("svc-b", "web")
    await service.apply("bots", plan)
    assert executor.ups() == [
        ("up", "-d", "--no-recreate", "svc-a", "db"),
        ("up", "-d", "--no-deps", "svc-b", "web"),
    ]


@pytest.mark.asyncio
async def test_apply_without_config_hash_support_runs_one_plain_up() -> None:
    executor = stack_executor(deployed={"app-a", "app-b"}, containers={"c-a": "svc-a"})
    service = ComposeAdminService(two_app_config(), executor_factory=lambda _: executor)

    plan = await service.plan_apply("bots")

    assert plan.unchanged is None
    await service.apply("bots", plan)
    assert executor.ups() == [("up", "-d", "svc-a", "svc-b", "db", "web")]


def test_apply_plan_rendering_splits_changed_and_unchanged() -> None:
    from botman.commands.compose_apply import render_apply_plan
    from botman.compose import ComposeApplyPlan

    text = render_apply_plan(
        ComposeApplyPlan(
            stack_name="bots",
            digest="x",
            services=("svc-a", "db"),
            skipped=(),
            orphans=(),
            unchanged=("db",),
        )
    )

    assert "Create or recreate (new or changed config): `svc-a`" in text
    assert "Keep as is, start if stopped: `db`" in text
