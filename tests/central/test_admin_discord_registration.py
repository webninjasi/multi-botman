from __future__ import annotations

import inspect
import sys

import pytest
from types import SimpleNamespace

from botman.commands.admin import register_admin_commands
from botman.compose import StackLockRegistry
from botman.config import ConfigStore


class FakeGroup:
    def __init__(self, *, name, description):
        self.name = name
        self.description = description
        self.children = []

    def command(self, *, name, description):
        def decorator(callback):
            callback.__command_name__ = name
            self.children.append(callback)
            return callback

        return decorator

    def add_command(self, command):
        self.children.append(command)


class FakeTree:
    def __init__(self):
        self.commands = []

    def add_command(self, command):
        self.commands.append(command)


class FakeBot:
    def __init__(self):
        self.tree = FakeTree()


def test_admin_command_groups_register_without_import_time_discord_dependency(
    tmp_path, monkeypatch
) -> None:
    fake_app_commands = SimpleNamespace(Group=FakeGroup)
    fake_discord = SimpleNamespace(
        app_commands=fake_app_commands,
        Interaction=type("Interaction", (), {}),
        Attachment=type("Attachment", (), {}),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    bot = FakeBot()
    store = ConfigStore(tmp_path / "config.yaml")
    register_admin_commands(
        bot,
        store,
        admin_ids="1, 2",
        locks=StackLockRegistry(),
    )

    assert [group.name for group in bot.tree.commands] == ["config", "env"]
    config_group = bot.tree.commands[0]
    assert [child.name for child in config_group.children if isinstance(child, FakeGroup)] == [
        "server",
        "stack",
        "app",
        "compose",
        "git",
        "agent",
    ]


def test_stack_scoped_admin_commands_do_not_expose_stack_option(tmp_path, monkeypatch) -> None:
    fake_app_commands = SimpleNamespace(Group=FakeGroup)
    fake_discord = SimpleNamespace(
        app_commands=fake_app_commands,
        Interaction=type("Interaction", (), {}),
        Attachment=type("Attachment", (), {}),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    bot = FakeBot()
    register_admin_commands(
        bot,
        ConfigStore(tmp_path / "config.yaml"),
        admin_ids="1",
        locks=StackLockRegistry(),
    )
    config_group = bot.tree.commands[0]
    groups = {child.name: child for child in config_group.children if isinstance(child, FakeGroup)}

    app_add = next(c for c in groups["app"].children if c.__command_name__ == "add")
    compose_upload = next(c for c in groups["compose"].children if c.__command_name__ == "upload")
    compose_show = next(c for c in groups["compose"].children if c.__command_name__ == "show")
    git_setup = next(c for c in groups["git"].children if c.__command_name__ == "setup")

    assert "stack" not in inspect.signature(app_add).parameters
    assert "stack" not in inspect.signature(compose_upload).parameters
    assert "stack" not in inspect.signature(compose_show).parameters
    assert "stack" not in inspect.signature(git_setup).parameters


def test_config_edit_commands_are_registered_and_stack_scoped(tmp_path, monkeypatch) -> None:
    fake_app_commands = SimpleNamespace(Group=FakeGroup)
    fake_discord = SimpleNamespace(
        app_commands=fake_app_commands,
        Interaction=type("Interaction", (), {}),
        Attachment=type("Attachment", (), {}),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    bot = FakeBot()
    register_admin_commands(
        bot,
        ConfigStore(tmp_path / "config.yaml"),
        admin_ids="1",
        locks=StackLockRegistry(),
    )
    config_group = bot.tree.commands[0]
    groups = {child.name: child for child in config_group.children if isinstance(child, FakeGroup)}

    server_names = {c.__command_name__ for c in groups["server"].children}
    stack_names = {c.__command_name__ for c in groups["stack"].children}
    app_names = {c.__command_name__ for c in groups["app"].children}
    assert {"add", "edit", "test"} <= server_names
    assert {"add", "edit"} <= stack_names
    assert {"add", "edit"} <= app_names

    stack_edit = next(c for c in groups["stack"].children if c.__command_name__ == "edit")
    app_edit = next(c for c in groups["app"].children if c.__command_name__ == "edit")
    assert "stack" not in inspect.signature(stack_edit).parameters
    assert "stack" not in inspect.signature(app_edit).parameters
    assert "channel_id" not in inspect.signature(stack_edit).parameters
    assert "channel_id" not in inspect.signature(app_edit).parameters

@pytest.mark.asyncio
async def test_agent_sync_uses_shared_server_lock(tmp_path, monkeypatch) -> None:
    import asyncio

    from botman.live_logs import ServerAgentLockRegistry
    from botman.models import BotmanConfig

    fake_app_commands = SimpleNamespace(Group=FakeGroup)
    fake_discord = SimpleNamespace(
        app_commands=fake_app_commands,
        Interaction=type("Interaction", (), {}),
        Attachment=type("Attachment", (), {}),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    calls = []

    class FakeAgentControlService:
        def __init__(self, config):
            self.config = config

        async def sync(self, server):
            calls.append(server)

    monkeypatch.setattr(
        "botman.commands.admin.AgentControlService", FakeAgentControlService
    )

    class Response:
        def __init__(self):
            self.done = False

        def is_done(self):
            return self.done

        async def defer(self, **kwargs):
            self.done = True

    class Followup:
        async def send(self, *args, **kwargs):
            return None

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=Response(),
        followup=Followup(),
    )
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(
        BotmanConfig.model_validate({"servers": {"vps1": {"type": "local"}}})
    )
    agent_locks = ServerAgentLockRegistry()
    bot = FakeBot()
    register_admin_commands(
        bot,
        store,
        admin_ids="1",
        locks=StackLockRegistry(),
        agent_locks=agent_locks,
    )
    config_group = bot.tree.commands[0]
    groups = {child.name: child for child in config_group.children if isinstance(child, FakeGroup)}
    sync_command = next(
        callback for callback in groups["agent"].children if callback.__command_name__ == "sync"
    )

    server_lock = agent_locks.get("vps1")
    await server_lock.acquire()
    task = asyncio.create_task(sync_command(interaction, "vps1"))
    await asyncio.sleep(0)
    assert calls == []
    assert not task.done()

    server_lock.release()
    await task
    assert calls == ["vps1"]
