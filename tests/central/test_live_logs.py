from pathlib import Path
from types import SimpleNamespace

import pytest

import botman.live_logs as mod
from botman.config import ConfigStore
from botman.live_logs import LiveLogsService


def write_config(path: Path):
    path.write_text(
        """
servers:
  vps1:
    type: local
stacks:
  s1:
    server: vps1
    channel_id: '10'
    project_name: p1
apps:
  app-a:
    stack: s1
    service: app-a
    log_identifier: tag-a
    git:
      repo_url: git@example.com:a.git
"""
    )


class AgentControl:
    calls = []
    fail = False

    def __init__(self, config):
        self.config = config

    async def sync(self, server_name):
        self.__class__.calls.append((server_name, self.config.apps["app-a"].log.live_enabled))
        if self.__class__.fail:
            raise RuntimeError("agent restart failed")


@pytest.mark.asyncio
async def test_live_start_persists_then_syncs(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path)
    AgentControl.calls = []
    AgentControl.fail = False
    monkeypatch.setattr(mod, "AgentControlService", AgentControl)
    service = LiveLogsService(ConfigStore(path))
    result = await service.start(
        app_name="app-a",
        channel_id=10,
        webhook_id="11",
        webhook_url="https://discord.example/hook",
        thread_id="22",
        subscription_id="sub",
    )
    assert result.enabled
    loaded = ConfigStore(path).load()
    assert loaded.apps["app-a"].log.live_enabled
    assert AgentControl.calls == [("vps1", True)]


@pytest.mark.asyncio
async def test_agent_sync_failure_rolls_back_central_state(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path)
    AgentControl.calls = []
    AgentControl.fail = True
    monkeypatch.setattr(mod, "AgentControlService", AgentControl)
    service = LiveLogsService(ConfigStore(path))
    with pytest.raises(RuntimeError, match="agent restart failed"):
        await service.start(
            app_name="app-a",
            channel_id=10,
            webhook_id="11",
            webhook_url="https://discord.example/hook",
            thread_id="22",
            subscription_id="sub",
        )
    loaded = ConfigStore(path).load()
    assert not loaded.apps["app-a"].log.live_enabled


@pytest.mark.asyncio
async def test_wrong_channel_fails_before_sync(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path)
    AgentControl.calls = []
    AgentControl.fail = False
    monkeypatch.setattr(mod, "AgentControlService", AgentControl)
    service = LiveLogsService(ConfigStore(path))
    with pytest.raises(PermissionError):
        await service.start(
            app_name="app-a",
            channel_id=999,
            webhook_id="11",
            webhook_url="https://discord.example/hook",
            thread_id="22",
            subscription_id="sub",
        )
    assert AgentControl.calls == []


@pytest.mark.asyncio
async def test_same_server_live_mutations_wait_for_agent_sync(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
servers:
  vps1:
    type: local
stacks:
  s1:
    server: vps1
    channel_id: '10'
    project_name: p1
apps:
  app-a:
    stack: s1
    service: app-a
    log_identifier: tag-a
    git:
      repo_url: git@example.com:a.git
  app-b:
    stack: s1
    service: app-b
    log_identifier: tag-b
    git:
      repo_url: git@example.com:b.git
"""
    )
    entered = __import__("asyncio").Event()
    release = __import__("asyncio").Event()
    snapshots = []

    class BlockingAgentControl:
        def __init__(self, config):
            self.config = config

        async def sync(self, server_name):
            snapshots.append(
                (
                    self.config.apps["app-a"].log.live_enabled,
                    self.config.apps["app-b"].log.live_enabled,
                )
            )
            if len(snapshots) == 1:
                entered.set()
                await release.wait()

    monkeypatch.setattr(mod, "AgentControlService", BlockingAgentControl)
    service = LiveLogsService(ConfigStore(path))

    import asyncio

    first = asyncio.create_task(
        service.start(
            app_name="app-a",
            channel_id=10,
            webhook_id="11",
            webhook_url="https://discord.example/a",
            thread_id="21",
            subscription_id="sub-a",
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        service.start(
            app_name="app-b",
            channel_id=10,
            webhook_id="12",
            webhook_url="https://discord.example/b",
            thread_id="22",
            subscription_id="sub-b",
        )
    )
    await asyncio.sleep(0)

    # The second central mutation must not commit while the first server-wide
    # config is still being synchronized.
    mid = ConfigStore(path).load()
    assert mid.apps["app-a"].log.live_enabled
    assert not mid.apps["app-b"].log.live_enabled

    release.set()
    await asyncio.gather(first, second)
    assert snapshots == [(True, False), (True, True)]
