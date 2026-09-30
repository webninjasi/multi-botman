from collections import deque

import pytest

from botman_agent.config import AgentApp, AgentSettings
from botman_agent.discord_sink import FatalDiscordDestinationError
from botman_agent.journal import JournalRecord
from botman_agent.live import AppSuspendedError, run_live_app, supervise_live_app
from botman_agent.state import AppState, StateStore


class FakeStream:
    def __init__(self, identifier, records):
        self.identifier = identifier
        self._records = records
        self.opened = None
        self.closed = False

    async def open_tail(self):
        self.opened = ("tail", None)

    async def open_cursor(self, cursor):
        self.opened = ("cursor", cursor)

    def records(self):
        async def gen():
            for record in self._records:
                yield record
        return gen()

    async def close(self):
        self.closed = True


class Sink:
    def __init__(self, fail_at=None):
        self.sent = []
        self.fail_at = fail_at

    async def send(self, content):
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise FatalDiscordDestinationError("dead")
        self.sent.append(content)


@pytest.mark.asyncio
async def test_split_entry_checkpoint_commits_only_after_all_segments(tmp_path):
    store = StateStore(tmp_path)
    record = JournalRecord(message="a" * 300 + "\nafter", cursor="c2", realtime_usec=200)
    stream = FakeStream("tag", [record])
    sink = Sink()
    app = AgentApp(
        identifier="tag",
        enabled=True,
        webhook_url="https://discord.example/hook",
        thread_id="123",
        subscription_id="s1",
    )
    settings = AgentSettings(state_dir=tmp_path, discord_message_limit=256)
    await run_live_app(
        "app-a",
        app,
        settings,
        state_store=store,
        sink=sink,
        journal_factory=lambda _: stream,
        now_usec=lambda: 1_000,
    )
    assert len(sink.sent) == 2
    assert store.load("app-a").cursor == "c2"
    assert stream.closed


@pytest.mark.asyncio
async def test_partial_delivery_never_advances_cursor(tmp_path):
    store = StateStore(tmp_path)
    old = AppState(subscription_id="s1", cursor="old", last_realtime_usec=900)
    store.save("app-a", old)
    stream = FakeStream("tag", [JournalRecord("a" * 300 + "\nafter", "new", 1000)])
    sink = Sink(fail_at=1)
    app = AgentApp(
        identifier="tag",
        enabled=True,
        webhook_url="https://discord.example/hook",
        thread_id="123",
        subscription_id="s1",
    )
    settings = AgentSettings(state_dir=tmp_path, discord_message_limit=256, resume_max_age_sec=300)
    with pytest.raises(AppSuspendedError):
        await run_live_app(
            "app-a",
            app,
            settings,
            state_store=store,
            sink=sink,
            journal_factory=lambda _: stream,
            now_usec=lambda: 1_000,
        )
    assert store.load("app-a").cursor == "old"


@pytest.mark.asyncio
async def test_stale_checkpoint_posts_gap_marker_and_tails(tmp_path):
    store = StateStore(tmp_path)
    store.save("app-a", AppState(subscription_id="s1", cursor="old", last_realtime_usec=1))
    stream = FakeStream("tag", [])
    sink = Sink()
    app = AgentApp(
        identifier="tag",
        enabled=True,
        webhook_url="https://discord.example/hook",
        thread_id="123",
        subscription_id="s1",
    )
    settings = AgentSettings(state_dir=tmp_path, resume_max_age_sec=1)
    await run_live_app(
        "app-a",
        app,
        settings,
        state_store=store,
        sink=sink,
        journal_factory=lambda _: stream,
        now_usec=lambda: 5_000_000,
    )
    assert stream.opened == ("tail", None)
    assert "checkpoint was too old" in sink.sent[0]


@pytest.mark.asyncio
async def test_supervisor_restarts_unexpected_error_but_not_suspension():
    events = deque([RuntimeError("boom"), None])
    sleeps = []

    async def sleep(value):
        sleeps.append(value)

    async def runner():
        event = events.popleft()
        if event:
            raise event

    app = AgentApp(identifier="tag")
    await supervise_live_app("app-a", app, AgentSettings(), runner=runner, sleep=sleep)
    assert sleeps == [1.0]

    calls = 0

    async def suspended():
        nonlocal calls
        calls += 1
        raise AppSuspendedError("dead")

    await supervise_live_app("app-a", app, AgentSettings(), runner=suspended, sleep=sleep)
    assert calls == 1


@pytest.mark.asyncio
async def test_unusable_saved_cursor_falls_back_to_tail_and_clears_checkpoint(tmp_path):
    store = StateStore(tmp_path)
    store.save("app-a", AppState(subscription_id="s1", cursor="gone", last_realtime_usec=900))

    class CursorFailStream(FakeStream):
        async def open_cursor(self, cursor):
            self.opened = ("cursor-failed", cursor)
            raise RuntimeError("cursor not found")

    stream = CursorFailStream("tag", [])
    sink = Sink()
    app = AgentApp(
        identifier="tag",
        enabled=True,
        webhook_url="https://discord.example/hook",
        thread_id="123",
        subscription_id="s1",
    )
    settings = AgentSettings(state_dir=tmp_path, resume_max_age_sec=300)

    await run_live_app(
        "app-a",
        app,
        settings,
        state_store=store,
        sink=sink,
        journal_factory=lambda _: stream,
        now_usec=lambda: 1_000,
    )

    assert stream.opened == ("tail", None)
    assert store.load("app-a") is None
    assert "cursor is no longer available" in sink.sent[0]


@pytest.mark.asyncio
async def test_enabled_supervisor_restarts_when_stream_ends_normally():
    calls = 0
    sleeps = []

    async def sleep(value):
        sleeps.append(value)

    async def runner():
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        raise AppSuspendedError("stop test")

    app = AgentApp(
        identifier="tag",
        enabled=True,
        webhook_url="https://discord.example/hook",
        thread_id="123",
        subscription_id="s1",
    )
    await supervise_live_app("app-a", app, AgentSettings(), runner=runner, sleep=sleep)

    assert calls == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_corrupt_checkpoint_is_discarded_and_tails_with_marker(tmp_path):
    store = StateStore(tmp_path)
    path = store.path_for("app-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    stream = FakeStream("tag", [])
    sink = Sink()
    app = AgentApp(
        identifier="tag",
        enabled=True,
        webhook_url="https://discord.example/hook",
        thread_id="123",
        subscription_id="s1",
    )

    await run_live_app(
        "app-a",
        app,
        AgentSettings(state_dir=tmp_path),
        state_store=store,
        sink=sink,
        journal_factory=lambda _: stream,
        now_usec=lambda: 1_000,
    )

    assert stream.opened == ("tail", None)
    assert not path.exists()
    assert "checkpoint was corrupt" in sink.sent[0]
