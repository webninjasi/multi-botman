"""Live app task and supervisor: journal -> formatter -> Discord -> checkpoint."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .config import AgentApp, AgentSettings
from .discord_sink import DiscordWebhookSink, FatalDiscordDestinationError
from .formatting import gap_marker, render_entry
from .journal import CysystemdJournalStream
from .state import AppState, StateStore, choose_start

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
    state = state_store.load(app_name)
    decision = choose_start(
        state,
        subscription_id=app.subscription_id,
        now_realtime_usec=now_usec(),
        resume_max_age_sec=settings.resume_max_age_sec,
    )
    try:
        if decision.mode == "cursor":
            assert decision.cursor is not None
            await stream.open_cursor(decision.cursor)
        else:
            await stream.open_tail()
            if decision.gap_marker:
                try:
                    await sink.send(gap_marker(limit=settings.discord_message_limit))
                except FatalDiscordDestinationError as exc:
                    raise AppSuspendedError(str(exc)) from exc

        async for record in stream.records():
            segments = render_entry(record.message, limit=settings.discord_message_limit)
            try:
                for segment in segments:
                    await sink.send(segment.content)
            except FatalDiscordDestinationError as exc:
                raise AppSuspendedError(str(exc)) from exc
            # Commit only after *every* segment for this journal entry succeeds.
            state_store.save(
                app_name,
                AppState(
                    subscription_id=app.subscription_id,
                    cursor=record.cursor,
                    last_realtime_usec=record.realtime_usec,
                ),
            )
    finally:
        await stream.close()


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
            return
        except AppSuspendedError:
            LOG.exception("live logging suspended for %s until the agent is reconfigured/restarted", app_name)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("live logging task failed for %s; restarting", app_name)
            await sleep(delay)
            delay = min(30.0, delay * 2)
