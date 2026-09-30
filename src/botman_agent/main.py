"""Executable runtime for the target-side Botman live-log agent."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .config import AgentConfig, load_agent_config
from .discord_sink import DiscordWebhookSink
from .live import run_live_app, supervise_live_app
from .state import StateStore

LOG = logging.getLogger(__name__)


async def run_agent(config: AgentConfig, *, session) -> None:
    """Run one supervised task per enabled app using a shared HTTP session."""
    state_store = StateStore(config.settings.state_dir)
    tasks: list[asyncio.Task[None]] = []
    for app_name, app in config.apps.items():
        if not app.enabled:
            continue
        assert app.webhook_url is not None and app.thread_id is not None
        sink = DiscordWebhookSink(
            session,
            webhook_url=app.webhook_url,
            thread_id=app.thread_id,
            retry_initial_sec=config.settings.retry_initial_sec,
            retry_max_sec=config.settings.retry_max_sec,
        )

        async def runner(
            *,
            _name=app_name,
            _app=app,
            _sink=sink,
        ) -> None:
            await run_live_app(
                _name,
                _app,
                config.settings,
                state_store=state_store,
                sink=_sink,
            )

        tasks.append(
            asyncio.create_task(
                supervise_live_app(
                    app_name,
                    app,
                    config.settings,
                    runner=runner,
                ),
                name=f"botman-live-{app_name}",
            )
        )

    if not tasks:
        LOG.info("no enabled live-log subscriptions; waiting for service restart/reconfiguration")
        await asyncio.Event().wait()

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _async_main(config_path: Path) -> None:
    try:
        import aiohttp  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - runtime dependency.
        raise RuntimeError("aiohttp is required to run botman-log-agent") from exc

    config = load_agent_config(config_path)
    timeout = aiohttp.ClientTimeout(total=config.settings.request_timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await run_agent(config, session=session)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("BOTMAN_AGENT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    path = Path(os.environ.get("BOTMAN_AGENT_CONFIG", "/var/lib/botman-log-agent/config.yaml"))
    try:
        asyncio.run(_async_main(path))
    except KeyboardInterrupt:  # clean systemd/console shutdown
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
