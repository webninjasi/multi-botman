"""Hybrid `/livelogs start|stop` Discord adapter."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from ..live_logs import LiveLogsService
from ..routing import ChannelAuthorizationError

LiveLogsServiceProvider = Callable[[], LiveLogsService]


class LiveLogsCommandAdapter:
    def __init__(self, service: LiveLogsService | LiveLogsServiceProvider):
        self._service = service

    def _get_service(self) -> LiveLogsService:
        if callable(self._service) and not hasattr(self._service, "start"):
            return self._service()
        return self._service  # type: ignore[return-value]

    async def start(self, ctx: Any, app_name: str) -> None:
        channel = getattr(ctx, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await self._send(ctx, "This command must be used in a configured stack channel.")
            return
        defer = getattr(ctx, "defer", None)
        if callable(defer):
            await defer()
        service = self._get_service()
        try:
            resolved = service.authorize(app_name, channel_id)
        except ChannelAuthorizationError:
            await self._send(ctx, "That app is not managed from this channel.")
            return
        except KeyError:
            await self._send(ctx, f"Unknown app: `{app_name}`")
            return

        try:
            webhook_id, webhook_url = await self._ensure_webhook(channel, resolved)
            thread, replacement = await self._ensure_thread(ctx, channel, resolved)
            old = resolved.app.log
            keep_subscription = (
                old.live_enabled
                and not replacement
                and old.thread_id == str(thread.id)
                and old.subscription_id is not None
            )
            subscription_id = old.subscription_id if keep_subscription else uuid.uuid4().hex
            await service.start(
                app_name=app_name,
                channel_id=channel_id,
                webhook_id=str(webhook_id),
                webhook_url=str(webhook_url),
                thread_id=str(thread.id),
                subscription_id=str(subscription_id),
            )
        except Exception as exc:
            await self._send(ctx, f"Failed to enable live logs for `{app_name}`: {exc}")
            return
        await self._send(
            ctx,
            f"Live logs enabled for `{app_name}` in <#{thread.id}>.",
        )

    async def stop(self, ctx: Any, app_name: str) -> None:
        channel = getattr(ctx, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await self._send(ctx, "This command must be used in a configured stack channel.")
            return
        defer = getattr(ctx, "defer", None)
        if callable(defer):
            await defer()
        service = self._get_service()
        try:
            resolved = service.authorize(app_name, channel_id)
            old_thread_id = resolved.app.log.thread_id
            await service.stop(app_name=app_name, channel_id=channel_id)
        except ChannelAuthorizationError:
            await self._send(ctx, "That app is not managed from this channel.")
            return
        except KeyError:
            await self._send(ctx, f"Unknown app: `{app_name}`")
            return
        except Exception as exc:
            await self._send(ctx, f"Failed to disable live logs for `{app_name}`: {exc}")
            return

        if old_thread_id:
            try:
                thread = await ctx.bot.fetch_channel(int(old_thread_id))
                if not getattr(thread, "locked", False):
                    await thread.edit(archived=True, reason=f"Botman live logs stopped for {app_name}")
            except Exception:
                pass
        await self._send(ctx, f"Live logs disabled for `{app_name}`.")

    async def _ensure_webhook(self, channel: Any, resolved) -> tuple[int | str, str]:
        old = resolved.app.log
        if old.webhook_id:
            try:
                webhooks = await channel.webhooks()
                for webhook in webhooks:
                    if str(webhook.id) == old.webhook_id and getattr(webhook, "url", None):
                        return webhook.id, str(webhook.url)
            except Exception:
                pass
        create = getattr(channel, "create_webhook", None)
        if not callable(create):
            raise RuntimeError("this channel does not support webhooks")
        webhook = await create(name=f"Botman logs — {resolved.name}"[:80])
        return webhook.id, str(webhook.url)

    async def _ensure_thread(self, ctx: Any, channel: Any, resolved) -> tuple[Any, bool]:
        old = resolved.app.log
        if old.live_enabled and old.thread_id:
            try:
                thread = await ctx.bot.fetch_channel(int(old.thread_id))
                if getattr(thread, "parent_id", channel.id) != channel.id:
                    raise RuntimeError("stored live-log thread belongs to another channel")
                if getattr(thread, "locked", False):
                    raise RuntimeError("stored live-log thread is locked")
                if getattr(thread, "archived", False):
                    thread = await thread.edit(
                        archived=False,
                        reason=f"Botman live logs resumed for {resolved.name}",
                    )
                return thread, False
            except Exception:
                pass
        create = getattr(channel, "create_thread", None)
        if not callable(create):
            raise RuntimeError("this channel does not support public threads")
        name = f"logs-{resolved.name}"[:100]
        try:
            import discord  # type: ignore[import-not-found]

            thread = await create(name=name, type=discord.ChannelType.public_thread)
        except ImportError:
            thread = await create(name=name)
        return thread, True

    @staticmethod
    async def _send(target: Any, content: str) -> None:
        kwargs: dict[str, Any] = {}
        try:
            import discord  # type: ignore[import-not-found]

            kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        except ImportError:
            pass
        await target.send(content, **kwargs)


def register_live_logs_commands(bot: Any, adapter: LiveLogsCommandAdapter) -> None:
    async def root(ctx: Any) -> None:
        await ctx.send("Use `livelogs start APP` or `livelogs stop APP`.")

    root.__name__ = "botman_livelogs"
    group = bot.hybrid_group(
        name="livelogs",
        description="Control persistent live journald delivery for an app.",
    )(root)

    async def start(ctx: Any, app: str) -> None:
        await adapter.start(ctx, app)

    async def stop(ctx: Any, app: str) -> None:
        await adapter.stop(ctx, app)

    start.__name__ = "botman_livelogs_start"
    stop.__name__ = "botman_livelogs_stop"
    group.command(name="start", description="Enable or repair live logs for an app.")(start)
    group.command(name="stop", description="Disable live logs for an app.")(stop)
