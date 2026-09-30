"""Hybrid historical `/logs tail|download` Discord adapter."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any, Literal

from ..logs import DEFAULT_EXPORT_PART_BYTES, HistoricalLogsService
from ..routing import ChannelAuthorizationError

HistoricalLogsServiceProvider = Callable[[], HistoricalLogsService]


class HistoricalLogsCommandAdapter:
    def __init__(self, service: HistoricalLogsService | HistoricalLogsServiceProvider):
        self._service = service

    def _get_service(self) -> HistoricalLogsService:
        if callable(self._service) and not hasattr(self._service, "tail"):
            return self._service()
        return self._service  # type: ignore[return-value]

    async def tail(self, ctx: Any, app_name: str, lines: int = 100) -> None:
        channel_id = getattr(getattr(ctx, "channel", None), "id", None)
        if channel_id is None:
            await self._send(ctx, "This command must be used in a configured stack channel.")
            return
        service = self._get_service()
        try:
            service.authorize(app_name, channel_id)
        except ChannelAuthorizationError:
            await self._send(ctx, "That app is not managed from this channel.")
            return
        except KeyError:
            await self._send(ctx, f"Unknown app: `{app_name}`")
            return
        defer = getattr(ctx, "defer", None)
        if callable(defer):
            await defer()
        try:
            output = await service.tail(app_name=app_name, channel_id=channel_id, lines=lines)
        except Exception as exc:
            await self._send(ctx, f"Failed to retrieve logs for `{app_name}`: {exc}")
            return
        if not output:
            await self._send(ctx, f"No retained journal entries found for `{app_name}`.")
            return
        safe = output.replace("```", "``\u200b`")
        if len(safe) <= 1750:
            await self._send(ctx, f"Recent logs for `{app_name}`:\n```text\n{safe}\n```")
            return
        await self._send_file(
            ctx,
            f"Recent logs for `{app_name}` are attached.",
            output.encode("utf-8", errors="replace"),
            f"{app_name}-tail.log",
        )

    async def download(
        self,
        ctx: Any,
        app_name: str,
        from_time: str,
        to_time: str,
        format: Literal["human", "jsonl"] = "human",
    ) -> None:
        channel_id = getattr(getattr(ctx, "channel", None), "id", None)
        if channel_id is None:
            await self._send(ctx, "This command must be used in a configured stack channel.")
            return
        service = self._get_service()
        try:
            service.authorize(app_name, channel_id)
        except ChannelAuthorizationError:
            await self._send(ctx, "That app is not managed from this channel.")
            return
        except KeyError:
            await self._send(ctx, f"Unknown app: `{app_name}`")
            return
        defer = getattr(ctx, "defer", None)
        if callable(defer):
            await defer()

        budget = self._upload_budget(ctx)
        try:
            result = await service.download(
                app_name=app_name,
                channel_id=channel_id,
                from_local=from_time,
                to_local=to_time,
                format=format,
                max_part_bytes=budget,
            )
        except Exception as exc:
            await self._send(ctx, f"Failed to export logs for `{app_name}`: {exc}")
            return

        if not result.artifacts:
            await self._send(ctx, f"No export artifact was produced for `{app_name}`.")
            return
        # Discord accepts a bounded number of files per message. Keep batches
        # small and independent rather than relying on one giant response.
        for index in range(0, len(result.artifacts), 10):
            batch = result.artifacts[index : index + 10]
            files = [self._discord_file(item.data, item.filename) for item in batch]
            prefix = (
                f"Historical logs for `{app_name}`: {result.count} retained journal "
                f"entr{'y' if result.count == 1 else 'ies'}."
                if index == 0
                else f"Additional export parts for `{app_name}`."
            )
            await self._send(ctx, prefix, files=files)

    @staticmethod
    def _upload_budget(ctx: Any) -> int:
        guild = getattr(ctx, "guild", None)
        limit = getattr(guild, "filesize_limit", None)
        if isinstance(limit, int) and limit > 1_000_000:
            return max(512_000, limit - 512_000)
        return DEFAULT_EXPORT_PART_BYTES

    @staticmethod
    def _discord_file(data: bytes, filename: str):
        try:
            import discord  # type: ignore[import-not-found]

            return discord.File(io.BytesIO(data), filename=filename)
        except ImportError:  # tests can inspect a simple tuple without discord.py.
            return (filename, data)

    async def _send_file(self, ctx: Any, content: str, data: bytes, filename: str) -> None:
        await self._send(ctx, content, file=self._discord_file(data, filename))

    @staticmethod
    async def _send(target: Any, content: str, **kwargs: Any) -> None:
        try:
            import discord  # type: ignore[import-not-found]

            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        except ImportError:
            pass
        await target.send(content, **kwargs)


def register_logs_commands(bot: Any, adapter: HistoricalLogsCommandAdapter) -> None:
    async def root(ctx: Any) -> None:
        await ctx.send("Use `logs tail APP [lines]` or `logs download APP FROM TO [human|jsonl]`.")

    root.__name__ = "botman_logs"
    group = bot.hybrid_group(name="logs", description="Read retained journald logs.")(root)

    async def tail(ctx: Any, app: str, lines: int = 100) -> None:
        await adapter.tail(ctx, app, lines)

    async def download(
        ctx: Any,
        app: str,
        from_time: str,
        to_time: str,
        format: Literal["human", "jsonl"] = "human",
    ) -> None:
        await adapter.download(ctx, app, from_time, to_time, format)

    tail.__name__ = "botman_logs_tail"
    download.__name__ = "botman_logs_download"
    group.command(name="tail", description="Show recent retained journal entries for an app.")(tail)
    group.command(name="download", description="Download retained logs for a local-time range.")(
        download
    )
