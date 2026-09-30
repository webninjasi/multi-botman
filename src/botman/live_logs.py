"""Central persistent live-log state transitions and target agent synchronization."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from .agent_control import AgentControlService
from .config import ConfigStore
from .models import LogConfig
from .routing import ResolvedApp, authorize_app_channel


@dataclass(frozen=True, slots=True)
class LiveLogResult:
    app_name: str
    enabled: bool
    thread_id: str | None
    subscription_id: str | None


class ServerAgentLockRegistry:
    """Serialize central state changes which rewrite one target agent config."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def get(self, server_name: str) -> asyncio.Lock:
        return self._locks[server_name]


class LiveLogsService:
    def __init__(self, store: ConfigStore, *, locks: ServerAgentLockRegistry | None = None):
        self.store = store
        self.locks = locks or ServerAgentLockRegistry()

    def authorize(self, app_name: str, channel_id: str | int) -> ResolvedApp:
        return authorize_app_channel(self.store.load_or_default(), app_name, channel_id)

    async def start(
        self,
        *,
        app_name: str,
        channel_id: str | int,
        webhook_id: str,
        webhook_url: str,
        thread_id: str,
        subscription_id: str,
    ) -> LiveLogResult:
        # Resolve once before taking the lock so wrong-channel requests fail
        # without waiting behind an unrelated target restart.
        server_name = self.authorize(app_name, channel_id).server_name
        async with self.locks.get(server_name):
            previous: LogConfig | None = None

            def mutate(config):
                nonlocal previous
                resolved = authorize_app_channel(config, app_name, channel_id)
                if resolved.server_name != server_name:
                    raise RuntimeError("app server changed while enabling live logs; retry the command")
                previous = resolved.app.log.model_copy(deep=True)
                resolved.app.log = LogConfig(
                    webhook_id=webhook_id,
                    webhook_url=webhook_url,
                    live_enabled=True,
                    thread_id=thread_id,
                    subscription_id=subscription_id,
                )

            await self.store.mutate(mutate)
            try:
                # The server lock spans mutation + target synchronization so
                # concurrent app changes on the same VPS cannot race and write
                # an older complete agent config after a newer one.
                await AgentControlService(self.store.load()).sync(server_name)
            except Exception:
                assert previous is not None
                await self._rollback_locked(app_name, previous, server_name)
                raise
        return LiveLogResult(app_name, True, thread_id, subscription_id)

    async def stop(
        self,
        *,
        app_name: str,
        channel_id: str | int,
    ) -> LiveLogResult:
        server_name = self.authorize(app_name, channel_id).server_name
        async with self.locks.get(server_name):
            previous: LogConfig | None = None

            def mutate(config):
                nonlocal previous
                resolved = authorize_app_channel(config, app_name, channel_id)
                if resolved.server_name != server_name:
                    raise RuntimeError("app server changed while disabling live logs; retry the command")
                previous = resolved.app.log.model_copy(deep=True)
                resolved.app.log = LogConfig(
                    webhook_id=resolved.app.log.webhook_id,
                    webhook_url=resolved.app.log.webhook_url,
                    live_enabled=False,
                    thread_id=None,
                    subscription_id=None,
                )

            await self.store.mutate(mutate)
            try:
                await AgentControlService(self.store.load()).sync(server_name)
            except Exception:
                assert previous is not None
                await self._rollback_locked(app_name, previous, server_name)
                raise
        return LiveLogResult(app_name, False, None, None)

    async def _rollback_locked(
        self, app_name: str, previous: LogConfig, server_name: str
    ) -> None:
        """Restore central and target state while the caller holds server lock."""

        def rollback(config):
            if app_name in config.apps:
                config.apps[app_name].log = previous.model_copy(deep=True)

        await self.store.mutate(rollback)
        # Best-effort restore of the previous target config/service. Preserve the
        # original exception if restoration itself also fails.
        try:
            await AgentControlService(self.store.load()).sync(server_name)
        except Exception:
            pass
