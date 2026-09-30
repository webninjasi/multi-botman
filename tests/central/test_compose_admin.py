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
        if args[:2] == ("mv", "--"):
            src, dst = args[-2:]
            self.files[dst] = self.files.pop(src)
        elif args[:3] == ("rm", "-f", "--"):
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
    assert any(call[:2] == ("mv", "--") for call in executor.calls)


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
    assert not any(call[:2] == ("mv", "--") for call in executor.calls)
    assert not any(".botman-" in path for path in executor.files)


@pytest.mark.asyncio
async def test_compose_show_reads_utf8_content() -> None:
    cfg = config()
    executor = MemoryExecutor()
    target = "/srv/botman/stacks/bots/compose.yml"
    executor.files[target] = VALID
    service = ComposeAdminService(cfg, executor_factory=lambda _: executor)
    assert await service.show("bots") == VALID.decode()
