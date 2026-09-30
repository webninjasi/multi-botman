from pathlib import Path

import pytest
import yaml

import botman.agent_control as mod
from botman.agent_control import AgentControlService, render_server_agent_config
from botman.executor import ExecResult
from botman.models import BotmanConfig


def config():
    return BotmanConfig.model_validate(
        {
            "servers": {"vps1": {"type": "local"}, "vps2": {"type": "local"}},
            "stacks": {
                "s1": {"server": "vps1", "channel_id": "1", "project_name": "p1"},
                "s2": {"server": "vps2", "channel_id": "2", "project_name": "p2"},
            },
            "apps": {
                "app-a": {
                    "stack": "s1",
                    "service": "a",
                    "log_identifier": "tag-a",
                    "git": {"repo_url": "git@example.com:a.git"},
                    "log": {
                        "live_enabled": True,
                        "webhook_id": "11",
                        "webhook_url": "https://discord.example/a",
                        "thread_id": "22",
                        "subscription_id": "sub",
                    },
                },
                "app-b": {
                    "stack": "s1",
                    "service": "b",
                    "log_identifier": "tag-b",
                    "git": {"repo_url": "git@example.com:b.git"},
                },
                "app-c": {
                    "stack": "s2",
                    "service": "c",
                    "log_identifier": "tag-c",
                    "git": {"repo_url": "git@example.com:c.git"},
                },
            },
        }
    )


def test_agent_config_contains_only_server_apps_and_only_active_secret():
    raw = yaml.safe_load(render_server_agent_config(config(), "vps1"))
    assert set(raw["apps"]) == {"app-a", "app-b"}
    assert raw["apps"]["app-a"]["webhook_url"] == "https://discord.example/a"
    assert raw["apps"]["app-a"]["identifier"] == "tag-a"
    assert raw["settings"]["state_dir"] == "/var/lib/botman-log-agent/state"
    assert raw["apps"]["app-b"] == {"identifier": "tag-b", "enabled": False}
    assert "app-c" not in raw["apps"]


class Executor:
    def __init__(self):
        self.calls = []
        self.writes = []

    async def run(self, argv, *, timeout=None, check=False):
        self.calls.append(tuple(argv))
        if tuple(argv)[3:5] == ("is-active", "botman-log-agent.service"):
            return ExecResult(tuple(argv), 0, "active\n", "")
        return ExecResult(tuple(argv), 0, "", "")

    async def write_bytes(self, path, data, *, mode=0o600, atomic=True):
        self.writes.append((str(path), data, mode, atomic))


@pytest.mark.asyncio
async def test_sync_writes_protected_config_and_restarts(monkeypatch):
    executor = Executor()
    monkeypatch.setattr(mod, "executor_for_server", lambda server: executor)
    result = await AgentControlService(config()).sync("vps1")
    assert result.stdout.strip() == "active"
    assert executor.writes[0][0] == "/var/lib/botman-log-agent/config.yaml"
    assert executor.writes[0][2] == 0o640
    assert ("sudo", "-n", "systemctl", "restart", "botman-log-agent.service") in executor.calls
