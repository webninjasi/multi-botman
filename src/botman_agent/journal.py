"""Small cysystemd 2.x adapter used by live logging and later exports."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable


class JournalUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JournalRecord:
    message: str
    cursor: str
    realtime_usec: int


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class CysystemdJournalStream:
    """One filtered direct-async journald stream, with no producer queue."""

    def __init__(
        self,
        identifier: str,
        *,
        reader_factory: Callable[[], Any] | None = None,
        api: tuple[Any, Any] | None = None,
    ):
        self.identifier = identifier
        self._reader_factory = reader_factory
        self._api = api
        self._reader: Any | None = None
        self._skip_cursor_once: str | None = None

    def _load_api(self) -> tuple[Any, Any, Callable[[], Any]]:
        if self._api is not None and self._reader_factory is not None:
            mode, rule = self._api
            return mode, rule, self._reader_factory
        try:
            from cysystemd.async_reader import AsyncJournalReader  # type: ignore[import-not-found]
            from cysystemd.reader import JournalOpenMode, Rule  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - Linux runtime dependency.
            raise JournalUnavailableError(
                "cysystemd 2.x is required to run the Botman log agent"
            ) from exc
        return JournalOpenMode, Rule, AsyncJournalReader

    async def _close_reader(self, reader: Any) -> None:
        close = getattr(reader, "close", None)
        if close is not None:
            await _maybe_await(close())

    async def open_tail(self) -> None:
        mode, rule_type, factory = self._load_api()
        reader = factory()
        try:
            await _maybe_await(reader.open(mode.LOCAL_ONLY))
            await _maybe_await(reader.add_filter(rule_type("SYSLOG_IDENTIFIER", self.identifier)))
            # Full MESSAGE data is required so the formatter, rather than libsystemd,
            # owns Discord-only truncation behavior.
            reader.data_threshold = 0
            await _maybe_await(reader.seek_tail())
        except Exception:
            await self._close_reader(reader)
            raise
        self._reader = reader
        self._skip_cursor_once = None

    async def open_cursor(self, cursor: str) -> None:
        mode, rule_type, factory = self._load_api()
        reader = factory()
        try:
            await _maybe_await(reader.open(mode.LOCAL_ONLY))
            await _maybe_await(reader.add_filter(rule_type("SYSLOG_IDENTIFIER", self.identifier)))
            reader.data_threshold = 0
            # cysystemd 2.x expects a bytes cursor, while checkpoints are stored as text.
            await _maybe_await(reader.seek_cursor(cursor.encode("utf-8")))
        except Exception:
            await self._close_reader(reader)
            raise
        self._reader = reader
        # sd-journal seek semantics may position at the checkpoint itself. Skip
        # it once if observed; if iteration starts after it, this is a no-op.
        self._skip_cursor_once = cursor

    async def records(self) -> AsyncIterator[JournalRecord]:
        if self._reader is None:
            raise RuntimeError("journal stream is not open")
        async for entry in self._reader:
            cursor_value = entry.cursor
            if isinstance(cursor_value, bytes):
                cursor = cursor_value.decode("utf-8", errors="strict")
            else:
                cursor = str(cursor_value)
            if self._skip_cursor_once is not None and cursor == self._skip_cursor_once:
                self._skip_cursor_once = None
                continue
            self._skip_cursor_once = None
            raw_message = entry.data.get("MESSAGE", "")
            if isinstance(raw_message, bytes):
                message = raw_message.decode("utf-8", errors="replace")
            else:
                message = str(raw_message)
            yield JournalRecord(
                message=message,
                cursor=cursor,
                realtime_usec=int(entry.get_realtime_usec()),
            )

    async def close(self) -> None:
        if self._reader is None:
            return
        reader = self._reader
        self._reader = None
        await self._close_reader(reader)
