from __future__ import annotations

import pytest

from botman.commands.update import UpdateCommandAdapter, register_update_command
from botman.deployment import DeploymentError, DeploymentResult, DeploymentTranscript
from botman.routing import ChannelAuthorizationError


class FakeThread:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeChannel:
    def __init__(self, channel_id=111) -> None:
        self.id = channel_id
        self.threads = []

    async def create_thread(self, **kwargs):
        thread = FakeThread()
        self.threads.append((kwargs, thread))
        return thread


class FakeContext:
    def __init__(self, channel_id=111) -> None:
        self.channel = FakeChannel(channel_id)
        self.sent = []
        self.deferred = 0

    async def defer(self):
        self.deferred += 1

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeDeploymentService:
    def __init__(self, *, reject=False, fail=False) -> None:
        self.reject = reject
        self.fail = fail
        self.calls = []

    async def update(self, *, app_name, channel_id, progress=None):
        self.calls.append((app_name, channel_id))
        if self.reject:
            raise ChannelAuthorizationError("wrong")
        await progress("fetching app")
        if self.fail:
            transcript = DeploymentTranscript(["fetching app", "boom"])
            raise DeploymentError("boom", transcript)
        return DeploymentResult("deployed", app_name, "a" * 40, None, b"full transcript\n")


class FakeBot:
    def __init__(self) -> None:
        self.commands = {}

    def hybrid_command(self, *, name, description):
        def decorator(callback):
            self.commands[name] = callback
            return callback

        return decorator


@pytest.mark.asyncio
async def test_update_creates_thread_only_after_service_authorizes_and_streams_progress() -> None:
    service = FakeDeploymentService()
    adapter = UpdateCommandAdapter(service)
    ctx = FakeContext(111)

    await adapter.invoke(ctx, "app-a")

    assert service.calls == [("app-a", 111)]
    assert ctx.sent == []
    assert len(ctx.channel.threads) == 1
    _, thread = ctx.channel.threads[0]
    assert thread.sent[0][0] == "fetching app"
    assert "Deployment complete" in thread.sent[-1][0]


@pytest.mark.asyncio
async def test_update_wrong_channel_creates_no_thread() -> None:
    service = FakeDeploymentService(reject=True)
    adapter = UpdateCommandAdapter(service)
    ctx = FakeContext(999)

    await adapter.invoke(ctx, "app-a")

    assert ctx.channel.threads == []
    assert ctx.sent[0][0] == "That app is not managed from this channel."


@pytest.mark.asyncio
async def test_update_failure_attaches_to_existing_progress_thread() -> None:
    service = FakeDeploymentService(fail=True)
    ctx = FakeContext()
    await UpdateCommandAdapter(service).invoke(ctx, "app-a")
    assert len(ctx.channel.threads) == 1
    _, thread = ctx.channel.threads[0]
    assert "Deployment failed" in thread.sent[-1][0]


def test_update_registers_as_hybrid_command() -> None:
    bot = FakeBot()
    register_update_command(bot, UpdateCommandAdapter(FakeDeploymentService()))
    assert set(bot.commands) == {"update"}
