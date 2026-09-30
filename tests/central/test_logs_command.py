from types import SimpleNamespace

import pytest

from botman.commands.logs import HistoricalLogsCommandAdapter, register_logs_commands
from botman.logs import DownloadResult, LogArtifact
from botman.routing import ChannelAuthorizationError


class Service:
    def __init__(self, authorized=True):
        self.authorized = authorized
        self.calls = []

    def authorize(self, app, channel):
        if not self.authorized:
            raise ChannelAuthorizationError("no")
        return SimpleNamespace()

    async def tail(self, **kwargs):
        self.calls.append(("tail", kwargs))
        return "hello"

    async def download(self, **kwargs):
        self.calls.append(("download", kwargs))
        return DownloadResult("app-a", 1, (LogArtifact("a.log.gz", b"x"),))


class Ctx:
    channel = SimpleNamespace(id=10)
    guild = SimpleNamespace(filesize_limit=8_000_000)

    def __init__(self):
        self.sent = []
        self.deferred = False

    async def defer(self):
        self.deferred = True

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


@pytest.mark.asyncio
async def test_tail_wrong_channel_stops_before_target_call():
    service = Service(False)
    ctx = Ctx()
    await HistoricalLogsCommandAdapter(service).tail(ctx, "app-a", 10)
    assert service.calls == []
    assert not ctx.deferred


@pytest.mark.asyncio
async def test_download_passes_guild_upload_budget():
    service = Service()
    ctx = Ctx()
    await HistoricalLogsCommandAdapter(service).download(
        ctx, "app-a", "2026-09-30 10:00", "2026-09-30 11:00", "human"
    )
    kind, kwargs = service.calls[0]
    assert kind == "download"
    assert kwargs["max_part_bytes"] == 7_488_000
    assert ctx.sent


class FakeHybridGroup:
    def __init__(self):
        self.children = []

    def command(self, **kwargs):
        def decorator(fn):
            self.children.append(kwargs["name"])
            return fn
        return decorator


class FakeBot:
    def __init__(self):
        self.group = None

    def hybrid_group(self, **kwargs):
        def decorator(fn):
            self.group = FakeHybridGroup()
            return self.group
        return decorator


def test_registration_has_tail_and_download():
    bot = FakeBot()
    register_logs_commands(bot, HistoricalLogsCommandAdapter(Service()))
    assert bot.group.children == ["tail", "download"]
