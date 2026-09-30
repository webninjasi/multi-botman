import pytest

from botman_agent.discord_sink import DiscordWebhookSink, FatalDiscordDestinationError


class Response:
    def __init__(self, status, *, body=None, text=""):
        self.status = status
        self.body = body or {}
        self._text = text
        self.released = False

    async def json(self):
        return self.body

    async def text(self):
        return self._text

    def release(self):
        self.released = True


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def post(self, url, json):
        self.calls.append((url, json))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_mentions_disabled_and_thread_id_supplied():
    session = Session([Response(204)])
    sink = DiscordWebhookSink(
        session, webhook_url="https://discord.example/api/webhooks/1/token", thread_id="123"
    )
    await sink.send("hello")
    url, body = session.calls[0]
    assert "thread_id=123" in url and "wait=true" in url
    assert body["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_rate_limit_waits_then_retries_same_payload():
    sleeps = []

    async def sleep(value):
        sleeps.append(value)

    session = Session([Response(429, body={"retry_after": 2.5}), Response(204)])
    sink = DiscordWebhookSink(
        session,
        webhook_url="https://discord.example/api/webhooks/1/token",
        thread_id="123",
        sleep=sleep,
    )
    await sink.send("hello")
    assert sleeps == [2.5]
    assert len(session.calls) == 2
    assert session.calls[0][1] == session.calls[1][1]


@pytest.mark.asyncio
async def test_fatal_destination_does_not_retry_hot_loop():
    session = Session([Response(404, text="unknown webhook")])
    sink = DiscordWebhookSink(
        session, webhook_url="https://discord.example/api/webhooks/1/token", thread_id="123"
    )
    with pytest.raises(FatalDiscordDestinationError):
        await sink.send("hello")
    assert len(session.calls) == 1
