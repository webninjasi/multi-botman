"""Live app task and supervisor: journal -> batch -> Discord -> checkpoint."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .config import AgentApp, AgentSettings
from .discord_sink import DiscordWebhookSink, FatalDiscordDestinationError
from .formatting import LiveBatch, gap_marker
from .journal import CysystemdJournalStream, JournalRecord
from .state import AppState, CorruptStateError, StateStore, choose_start

LOG = logging.getLogger(__name__)


class AppSuspendedError(RuntimeError):
    """A fatal Discord destination error suspended one app until re-provision/restart."""


async def run_live_app(
    app_name: str,
    app: AgentApp,
    settings: AgentSettings,
    *,
    state_store: StateStore,
    sink: DiscordWebhookSink,
    journal_factory: Callable[[str], CysystemdJournalStream] = CysystemdJournalStream,
    now_usec: Callable[[], int] = lambda: time.time_ns() // 1_000,
) -> None:
    if not app.enabled:
        return
    assert app.subscription_id is not None
    stream = journal_factory(app.identifier)
    marker_reason: str | None = None
    try:
        state = state_store.load(app_name)
    except CorruptStateError:
        LOG.warning("discarding corrupt live-log checkpoint for %s", app_name, exc_info=True)
        state_store.delete(app_name)
        state = None
        marker_reason = "saved checkpoint was corrupt and could not be resumed"
    decision = choose_start(
        state,
        subscription_id=app.subscription_id,
        now_realtime_usec=now_usec(),
        resume_max_age_sec=settings.resume_max_age_sec,
    )
    try:
        if decision.mode == "cursor":
            assert decision.cursor is not None
            try:
                await stream.open_cursor(decision.cursor)
            except Exception:
                LOG.warning(
                    "saved journal cursor for %s is unavailable; falling back to live tail",
                    app_name,
                    exc_info=True,
                )
                await stream.open_tail()
                state_store.delete(app_name)
                marker_reason = "saved journal cursor is no longer available"
        else:
            await stream.open_tail()
            if decision.gap_marker:
                state_store.delete(app_name)
                marker_reason = "checkpoint was too old to resume safely"

        if marker_reason is not None:
            try:
                await sink.send(
                    gap_marker(
                        limit=settings.discord_message_limit,
                        reason=marker_reason,
                    )
                )
            except FatalDiscordDestinationError as exc:
                raise AppSuspendedError(str(exc)) from exc

        await _deliver_batched(
            app_name,
            app.subscription_id,
            stream,
            settings,
            state_store=state_store,
            sink=sink,
        )
    finally:
        await stream.close()


async def _deliver_batched(
    app_name: str,
    subscription_id: str,
    stream: CysystemdJournalStream,
    settings: AgentSettings,
    *,
    state_store: StateStore,
    sink: DiscordWebhookSink,
) -> None:
    """Batch entries into Discord messages, flushing when full or after a max delay.

    Memory stays bounded: at most one pending message of text plus one journal
    entry read ahead. The checkpoint is saved only after the message holding an
    entry's final segment is acknowledged, so a crash re-delivers (never skips)
    the unsent batch.
    """
    loop = asyncio.get_running_loop()
    batch = LiveBatch(limit=settings.discord_message_limit)
    max_delay = settings.batch_max_delay_sec
    # Checkpoint for the newest entry whose final segment sits in the pending batch.
    pending_state: AppState | None = None
    deadline: float | None = None

    async def send(content: str) -> None:
        try:
            await sink.send(content)
        except FatalDiscordDestinationError as exc:
            raise AppSuspendedError(str(exc)) from exc

    def commit() -> None:
        nonlocal pending_state
        if pending_state is not None:
            state_store.save(app_name, pending_state)
            pending_state = None

    async def flush() -> None:
        nonlocal deadline
        content = batch.take()
        if content is not None:
            await send(content)
        commit()
        deadline = None

    records = stream.records().__aiter__()
    next_record: asyncio.Future[JournalRecord] | None = None
    try:
        while True:
            if next_record is None:
                next_record = asyncio.ensure_future(anext(records))
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            done, _ = await asyncio.wait({next_record}, timeout=timeout)
            if not done:
                await flush()
                continue
            task, next_record = next_record, None
            try:
                record = task.result()
            except StopAsyncIteration:
                break
            ready = batch.add(record.message)
            for content in ready:
                await send(content)
            if ready:
                # The previously pending batch was among the acknowledged messages.
                commit()
            pending_state = AppState(
                subscription_id=subscription_id,
                cursor=record.cursor,
                last_realtime_usec=record.realtime_usec,
            )
            if max_delay <= 0:
                await flush()
            elif deadline is None:
                deadline = loop.time() + max_delay
        await flush()
    finally:
        if next_record is not None and not next_record.done():
            next_record.cancel()
            try:
                await next_record
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass


async def supervise_live_app(
    app_name: str,
    app: AgentApp,
    settings: AgentSettings,
    *,
    runner: Callable[[], object],
    sleep=asyncio.sleep,
) -> None:
    delay = 1.0
    while True:
        try:
            result = runner()
            if asyncio.iscoroutine(result):
                await result
            if not app.enabled:
                return
            LOG.warning("live logging stream ended unexpectedly for %s; restarting", app_name)
            await sleep(delay)
            delay = min(30.0, delay * 2)
        except AppSuspendedError:
            LOG.exception("live logging suspended for %s until the agent is reconfigured/restarted", app_name)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("live logging task failed for %s; restarting", app_name)
            await sleep(delay)
            delay = min(30.0, delay * 2)
