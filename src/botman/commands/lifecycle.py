"""Hybrid Discord lifecycle command adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..compose import LifecycleOperation, LifecycleService
from ..discord_output import bytes_file, present_exec_result
from ..executor import CommandError, CommandTimeout
from ..routing import ChannelAuthorizationError

LifecycleServiceProvider = Callable[[], LifecycleService]


class LifecycleCommandAdapter:
    """Thin Discord adapter. Authorization remains inside LifecycleService."""

    def __init__(self, service: LifecycleService | LifecycleServiceProvider):
        self._service = service

    def _get_service(self):
        if callable(self._service) and not hasattr(self._service, "execute"):
            return self._service()
        return self._service

    async def invoke(self, ctx: Any, operation: LifecycleOperation, app_name: str) -> None:
        channel = getattr(ctx, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await self._send(ctx, "This command must be used in a configured stack channel.")
            return

        defer = getattr(ctx, "defer", None)
        if callable(defer):
            await defer()

        try:
            result = await self._get_service().execute(
                operation,
                app_name=app_name,
                channel_id=channel_id,
            )
        except ChannelAuthorizationError:
            await self._send(ctx, "That app is not managed from this channel.")
            return
        except KeyError:
            await self._send(ctx, f"Unknown app: `{app_name}`")
            return
        except CommandTimeout as exc:
            await self._send(ctx, f"`{operation}` timed out after {exc.timeout:g}s for `{app_name}`.")
            return
        except CommandError as exc:
            presentation = present_exec_result(operation, app_name, exc.result)
            await self._send_presentation(ctx, presentation)
            return

        presentation = present_exec_result(operation, app_name, result)
        await self._send_presentation(ctx, presentation)

    async def _send_presentation(self, ctx: Any, presentation) -> None:
        kwargs: dict[str, Any] = {}
        if presentation.attachment_bytes is not None and presentation.attachment_name is not None:
            try:
                kwargs["file"] = bytes_file(
                    presentation.attachment_bytes,
                    presentation.attachment_name,
                )
            except ImportError:
                # Keeps transport-agnostic tests and config-only tooling usable.
                pass
        await self._send(ctx, presentation.content, **kwargs)

    @staticmethod
    async def _send(ctx: Any, content: str, **kwargs: Any) -> None:
        try:
            import discord  # type: ignore[import-not-found]

            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        except ImportError:
            pass
        await ctx.send(content, **kwargs)


def register_lifecycle_commands(bot: Any, adapter: LifecycleCommandAdapter) -> None:
    """Register /start, /stop, /restart, /status and their prefix equivalents."""

    descriptions = {
        "start": "Start one managed app service.",
        "stop": "Stop one managed app service.",
        "restart": "Restart one managed app service.",
        "status": "Show Compose status for one managed app service.",
    }

    def callback_for(operation: LifecycleOperation):
        async def callback(ctx: Any, app: str) -> None:
            await adapter.invoke(ctx, operation, app)

        callback.__name__ = f"botman_{operation}"
        callback.__doc__ = descriptions[operation]
        return callback

    for operation in ("start", "stop", "restart", "status"):
        bot.hybrid_command(name=operation, description=descriptions[operation])(
            callback_for(operation)
        )
