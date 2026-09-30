from __future__ import annotations

import inspect
import sys
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
