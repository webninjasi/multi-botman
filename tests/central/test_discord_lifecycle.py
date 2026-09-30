from __future__ import annotations

from types import SimpleNamespace

import pytest

from botman.commands.lifecycle import LifecycleCommandAdapter, register_lifecycle_commands
from botman.executor import ExecResult
from botman.routing import ChannelAuthorizationError


class FakeLifecycleService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.reject = False

    async def execute(self, operation, *, app_name, channel_id):
        self.calls.append((operation, app_name, channel_id))
        if self.reject:
            raise ChannelAuthorizationError("wrong channel")
        return ExecResult(("docker", "compose", operation, app_name), 0, "done", "")


class FakeContext:
    def __init__(self, channel_id: int = 123) -> None:
        self.channel = SimpleNamespace(id=channel_id)
        self.sent: list[tuple[str, dict]] = []
        self.deferred = 0

    async def defer(self):
        self.deferred += 1

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeBot:
    def __init__(self) -> None:
        self.commands = {}

    def hybrid_command(self, *, name, description):
        def decorator(callback):
            self.commands[name] = (callback, description)
            return callback

        return decorator


@pytest.mark.asyncio
async def test_lifecycle_adapter_passes_actual_context_channel_to_service() -> None:
    service = FakeLifecycleService()
    adapter = LifecycleCommandAdapter(lambda: service)
    ctx = FakeContext(777)

    await adapter.invoke(ctx, "restart", "app-a")

    assert service.calls == [("restart", "app-a", 777)]
    assert ctx.deferred == 1
    assert "restart `app-a`" in ctx.sent[0][0]
    assert "done" in ctx.sent[0][0]


@pytest.mark.asyncio
async def test_lifecycle_adapter_rejection_does_not_leak_expected_channel() -> None:
    service = FakeLifecycleService()
    service.reject = True
    adapter = LifecycleCommandAdapter(service)
    ctx = FakeContext(999)

    await adapter.invoke(ctx, "start", "app-a")

    assert service.calls == [("start", "app-a", 999)]
    assert ctx.sent[0][0] == "That app is not managed from this channel."
    assert "123" not in ctx.sent[0][0]


@pytest.mark.asyncio
async def test_registered_hybrid_callbacks_share_adapter_boundary() -> None:
    service = FakeLifecycleService()
    adapter = LifecycleCommandAdapter(service)
    bot = FakeBot()
    register_lifecycle_commands(bot, adapter)

    assert set(bot.commands) == {"start", "stop", "restart", "status"}
    for operation, (callback, description) in bot.commands.items():
        ctx = FakeContext(321)
        await callback(ctx, "app-a")
        assert service.calls[-1] == (operation, "app-a", 321)
        assert description
