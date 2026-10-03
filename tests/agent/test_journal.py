from types import SimpleNamespace

import pytest

from botman_agent.journal import CysystemdJournalStream


class Rule:
    def __init__(self, field, value):
        self.field = field
        self.value = value


class Entry:
    def __init__(self, cursor, message, usec):
        self.cursor = cursor
        self.data = {"MESSAGE": message}
        self.usec = usec

    def get_realtime_usec(self):
        return self.usec


class Reader:
    def __init__(self, entries):
        self.entries = entries
        self.opened = None
        self.filter = None
        self.data_threshold = 99
        self.seek = None
        self.closed = False

    async def open(self, mode):
        self.opened = mode

    def add_filter(self, rule):
        self.filter = rule

    async def seek_tail(self):
        self.seek = ("tail", None)

    async def seek_cursor(self, cursor):
        if not isinstance(cursor, bytes):
            raise TypeError("Argument 'cursor' has incorrect type (expected bytes, got str)")
        self.seek = ("cursor", cursor)

    def __aiter__(self):
        async def gen():
            for entry in self.entries:
                yield entry
        return gen()

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_journal_stream_filters_and_tail_opens_without_queue():
    reader = Reader([Entry(b"c1", "hello", 5)])
    stream = CysystemdJournalStream(
        "tag-a", reader_factory=lambda: reader, api=(SimpleNamespace(SYSTEM="system"), Rule)
    )
    await stream.open_tail()
    rows = [row async for row in stream.records()]
    assert reader.opened == "system"
    assert (reader.filter.field, reader.filter.value) == ("SYSLOG_IDENTIFIER", "tag-a")
    assert reader.data_threshold == 0
    assert reader.seek == ("tail", None)
    assert rows[0].cursor == "c1"
    assert rows[0].message == "hello"
    await stream.close()
    assert reader.closed


@pytest.mark.asyncio
async def test_cursor_checkpoint_itself_is_skipped_once():
    reader = Reader([Entry("c1", "duplicate", 1), Entry("c2", "next", 2)])
    stream = CysystemdJournalStream(
        "tag-a", reader_factory=lambda: reader, api=(SimpleNamespace(SYSTEM="system"), Rule)
    )
    await stream.open_cursor("c1")
    rows = [row async for row in stream.records()]
    assert reader.seek == ("cursor", b"c1")
    assert [r.cursor for r in rows] == ["c2"]


@pytest.mark.asyncio
async def test_failed_cursor_seek_closes_partially_opened_reader():
    class FailingReader(Reader):
        async def seek_cursor(self, cursor):
            raise RuntimeError("cursor unavailable")

    reader = FailingReader([])
    stream = CysystemdJournalStream(
        "tag-a", reader_factory=lambda: reader, api=(SimpleNamespace(SYSTEM="system"), Rule)
    )

    with pytest.raises(RuntimeError, match="cursor unavailable"):
        await stream.open_cursor("gone")

    assert reader.closed
