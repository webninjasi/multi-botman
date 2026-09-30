from __future__ import annotations

import pytest

from botman.env import EnvMissingError, EnvService, set_env_value, unset_env_value
from botman.executor import ExecResult
from botman.models import BotmanConfig


def config() -> BotmanConfig:
    return BotmanConfig.model_validate(
        {
            "servers": {"target": {"type": "local"}},
            "stacks": {
                "bots": {
                    "server": "target",
                    "channel_id": "1",
                    "project_name": "bots",
                }
            },
            "apps": {
                "app-a": {
                    "stack": "bots",
                    "service": "app-a",
                    "log_identifier": "bots-app-a",
                    "git": {"repo_url": "git@example.com:o/a.git"},
                }
            },
        }
    )


class MemoryExecutor:
    def __init__(self) -> None:
        self.files = {}
        self.modes = {}
        self.calls = []

    async def run(self, argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        self.calls.append(args)
        result = ExecResult(args, 0, "", "")
        return result.check() if check else result

    async def read_bytes(self, path):
        key = str(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    async def write_bytes(self, path, data, *, mode=0o600, atomic=True):
        self.files[str(path)] = data
        self.modes[str(path)] = mode


def test_line_preserving_env_edits() -> None:
    original = "# keep\nA=1\nexport B=2\nC=3"
    updated = set_env_value(original, "B", "hello world")
    assert updated == "# keep\nA=1\nB=hello world\nC=3"
    appended = set_env_value(updated, "D", "4")
    assert appended.endswith("C=3\nD=4\n")
    removed = unset_env_value(appended, "A")
    assert "# keep\n" in removed
    assert "A=1" not in removed
    assert "B=hello world" in removed


@pytest.mark.asyncio
async def test_env_service_distinguishes_missing_and_writes_0600() -> None:
    executor = MemoryExecutor()
    service = EnvService(config(), executor_factory=lambda _: executor)
    with pytest.raises(EnvMissingError):
        await service.show("app-a", 1)

    path = await service.set("app-a", 1, "TOKEN", "secret")
    assert executor.files[path] == b"TOKEN=secret\n"
    assert executor.modes[path] == 0o600

    shown = await service.show("app-a", 1)
    assert shown.content == "TOKEN=secret\n"
    await service.unset("app-a", 1, "TOKEN")
    assert executor.files[path] == b""

@pytest.mark.asyncio
async def test_env_mutation_waits_for_shared_stack_lock(tmp_path) -> None:
    import asyncio

    from botman.compose import StackLockRegistry
    from botman.config import ConfigStore

    cfg = config()
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(cfg)
    executor = MemoryExecutor()
    locks = StackLockRegistry()
    service = EnvService(
        store,
        executor_factory=lambda _: executor,
        locks=locks,
    )

    lock = await locks.get("bots")
    await lock.acquire()
    task = asyncio.create_task(service.set("app-a", 1, "TOKEN", "secret"))
    await asyncio.sleep(0)
    assert not task.done()
    assert executor.files == {}

    lock.release()
    path = await task
    assert executor.files[path] == b"TOKEN=secret\n"
