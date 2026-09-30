from types import SimpleNamespace

import pytest

from botman.commands.live_logs import LiveLogsCommandAdapter, register_live_logs_commands
from botman.routing import ChannelAuthorizationError


class Thread:
    def __init__(self, id=22, *, archived=False, locked=False, parent_id=10):
        self.id = id
        self.archived = archived
        self.locked = locked
        self.parent_id = parent_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.archived = kwargs.get("archived", self.archived)
        return self


class Webhook:
    id = 11
    url = "https://discord.example/hook"


class Channel:
    id = 10

    def __init__(self):
        self.created_threads = []
        self.created_webhooks = []

    async def webhooks(self):
        return []

    async def create_webhook(self, **kwargs):
        self.created_webhooks.append(kwargs)
        return Webhook()

    async def create_thread(self, **kwargs):
        self.created_threads.append(kwargs)
        return Thread()


class Bot:
    async def fetch_channel(self, id):
        raise RuntimeError("missing")


class Ctx:
    def __init__(self):
        self.channel = Channel()
        self.bot = Bot()
        self.sent = []
        self.deferred = False

    async def defer(self):
        self.deferred = True

    async def send(self, content, **kwargs):
        self.sent.append(content)


class Service:
    def __init__(self, authorized=True):
        self.calls = []
        self.authorized = authorized

    def authorize(self, app_name, channel_id):
        if not self.authorized:
            raise ChannelAuthorizationError("no")
        return SimpleNamespace(
            name=app_name,
            app=SimpleNamespace(
                log=SimpleNamespace(
                    webhook_id=None,
                    live_enabled=False,
                    thread_id=None,
                    subscription_id=None,
                )
            ),
        )

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))

    async def stop(self, **kwargs):
        self.calls.append(("stop", kwargs))


@pytest.mark.asyncio
async def test_start_authorizes_before_creating_discord_resources():
    service = Service(authorized=False)
    ctx = Ctx()
    await LiveLogsCommandAdapter(service).start(ctx, "app-a")
    assert ctx.channel.created_threads == []
    assert ctx.channel.created_webhooks == []
    assert service.calls == []


@pytest.mark.asyncio
async def test_start_creates_destination_and_calls_service():
    service = Service()
    ctx = Ctx()
    await LiveLogsCommandAdapter(service).start(ctx, "app-a")
    assert len(ctx.channel.created_threads) == 1
    assert len(ctx.channel.created_webhooks) == 1
    kind, call = service.calls[0]
    assert kind == "start"
    assert call["thread_id"] == "22"
    assert call["webhook_id"] == "11"
    assert len(call["subscription_id"]) == 32


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


def test_registration_has_start_and_stop_subcommands():
    bot = FakeBot()
    register_live_logs_commands(bot, LiveLogsCommandAdapter(Service()))
    assert bot.group.children == ["start", "stop"]
