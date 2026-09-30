"""Discord bot entrypoint and runtime wiring."""

from __future__ import annotations

import os
from pathlib import Path

from .commands.admin import register_admin_commands
from .commands.env import register_env_commands
from .commands.lifecycle import LifecycleCommandAdapter, register_lifecycle_commands
from .commands.live_logs import LiveLogsCommandAdapter, register_live_logs_commands
from .commands.logs import HistoricalLogsCommandAdapter, register_logs_commands
from .commands.update import UpdateCommandAdapter, register_update_command
from .compose import LifecycleService, StackLockRegistry
from .config import ConfigStore
from .deployment import DeploymentService
from .live_logs import LiveLogsService, ServerAgentLockRegistry
from .logs import HistoricalLogsService


def create_bot(config_path: str | Path):
    """Create the discord.py Bot without connecting it to Discord."""

    try:
        import discord  # type: ignore[import-not-found]
        from discord.ext import commands  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on runtime installation.
        raise RuntimeError("discord.py is required to run Botman") from exc

    store = ConfigStore(config_path)
    startup_config = store.load_or_default()
    intents = discord.Intents.default()
    intents.message_content = True

    class BotmanBot(commands.Bot):
        async def setup_hook(self) -> None:
            await self.tree.sync()

    bot = BotmanBot(
        command_prefix=startup_config.settings.command_prefix,
        intents=intents,
        help_command=None,
    )
    shared_locks = StackLockRegistry()
    agent_locks = ServerAgentLockRegistry()

    def lifecycle_service() -> LifecycleService:
        # Reload for every interaction so future admin config changes take
        # effect immediately without restarting the central bot.
        return LifecycleService(store, locks=shared_locks)

    register_lifecycle_commands(bot, LifecycleCommandAdapter(lifecycle_service))

    def deployment_service() -> DeploymentService:
        return DeploymentService(store, locks=shared_locks)

    register_update_command(bot, UpdateCommandAdapter(deployment_service))

    def live_logs_service() -> LiveLogsService:
        return LiveLogsService(store, locks=agent_locks)

    register_live_logs_commands(bot, LiveLogsCommandAdapter(live_logs_service))

    def historical_logs_service() -> HistoricalLogsService:
        return HistoricalLogsService(store.load_or_default())

    register_logs_commands(bot, HistoricalLogsCommandAdapter(historical_logs_service))
    register_env_commands(bot, store, locks=shared_locks)
    register_admin_commands(
        bot,
        store,
        admin_ids=os.environ.get("ADMIN_IDS"),
        locks=shared_locks,
        agent_locks=agent_locks,
    )
    return bot


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN is required")
    config_path = os.environ.get("BOTMAN_CONFIG", "/etc/botman/config.yaml")
    bot = create_bot(config_path)
    bot.run(token)


if __name__ == "__main__":  # pragma: no cover
    main()
