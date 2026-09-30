from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from botman.commands.env import register_env_commands
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


class FakeTree:
    def __init__(self):
        self.commands = []

    def add_command(self, command):
        self.commands.append(command)


class FakeBot:
    def __init__(self):
        self.tree = FakeTree()


class Response:
    def __init__(self):
        self.done = False
        self.defer_kwargs = None

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True
        self.defer_kwargs = kwargs


class Followup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


def install_fake_discord(monkeypatch) -> None:
    fake_app_commands = SimpleNamespace(Group=FakeGroup)
    fake_discord = SimpleNamespace(
        app_commands=fake_app_commands,
        Interaction=type("Interaction", (), {}),
        Attachment=type("Attachment", (), {}),
        File=lambda *args, **kwargs: (args, kwargs),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)


@pytest.mark.asyncio
async def test_env_set_is_not_admin_gated_and_uses_stack_channel(tmp_path, monkeypatch) -> None:
    install_fake_discord(monkeypatch)
    calls = []

    class FakeEnvService:
        def __init__(self, store, *, locks):
            self.store = store
            self.locks = locks

        async def set(self, app, channel_id, key, value):
            calls.append((app, channel_id, key, value))
            return "/srv/botman/stacks/parkour/env/tfmbot.env"

    monkeypatch.setattr("botman.commands.env.EnvService", FakeEnvService)

    bot = FakeBot()
    register_env_commands(
        bot,
        ConfigStore(tmp_path / "config.yaml"),
        locks=StackLockRegistry(),
    )
    assert [group.name for group in bot.tree.commands] == ["env"]
    env_group = bot.tree.commands[0]
    env_set = next(c for c in env_group.children if c.__command_name__ == "set")

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999999),  # deliberately unrelated to ADMIN_IDS
        channel_id=1234,
        response=Response(),
        followup=Followup(),
    )
    await env_set(interaction, "tfmbot", "TOKEN", "secret")

    assert calls == [("tfmbot", 1234, "TOKEN", "secret")]
    assert interaction.response.defer_kwargs == {"ephemeral": True, "thinking": True}
    assert interaction.followup.messages[-1][1]["ephemeral"] is True


def test_env_commands_register_separately_from_admin_commands(tmp_path, monkeypatch) -> None:
    install_fake_discord(monkeypatch)
    bot = FakeBot()
    register_env_commands(
        bot,
        ConfigStore(tmp_path / "config.yaml"),
        locks=StackLockRegistry(),
    )
    env_group = bot.tree.commands[0]
    assert {c.__command_name__ for c in env_group.children} == {
        "show",
        "upload",
        "set",
        "unset",
    }
