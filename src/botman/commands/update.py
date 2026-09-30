"""Hybrid /update and !update Discord adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..deployment import DeploymentError, DeploymentService
from ..discord_output import attachment_parts, discord_files
from ..git import GitError
from ..routing import ChannelAuthorizationError

DeploymentServiceProvider = Callable[[], DeploymentService]


class UpdateCommandAdapter:
    def __init__(self, service: DeploymentService | DeploymentServiceProvider):
        self._service = service

    def _get_service(self):
        if callable(self._service) and not hasattr(self._service, "update"):
            return self._service()
        return self._service

    async def invoke(self, ctx: Any, app_name: str) -> None:
        channel = getattr(ctx, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await self._send(ctx, "This command must be used in a configured stack channel.")
            return

        defer = getattr(ctx, "defer", None)
        if callable(defer):
            await defer()

        thread = None

        async def ensure_thread():
            nonlocal thread
            if thread is not None:
                return thread
            create_thread = getattr(channel, "create_thread", None)
            if not callable(create_thread):
                raise RuntimeError("this channel does not support deployment threads")
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            name = f"deploy-{app_name}-{timestamp}"[:100]
            try:
                import discord  # type: ignore[import-not-found]

                thread = await create_thread(name=name, type=discord.ChannelType.public_thread)
            except ImportError:
                thread = await create_thread(name=name)
            return thread

        async def progress(message: str) -> None:
            target = await ensure_thread()
            await self._send(target, message)

        try:
            result = await self._get_service().update(
                app_name=app_name,
                channel_id=channel_id,
                progress=progress,
            )
        except ChannelAuthorizationError:
            await self._send(ctx, "That app is not managed from this channel.")
            return
        except KeyError:
            await self._send(ctx, f"Unknown app: `{app_name}`")
            return
        except (DeploymentError, GitError) as exc:
            target = thread or await ensure_thread()
            transcript = (
                exc.transcript.render()
                if isinstance(exc, DeploymentError)
                else (f"Git failure: {exc}\n").encode()
            )
            await self._send_with_transcript(
                target,
                f"Deployment failed for `{app_name}`: {exc}",
                app_name,
                transcript,
            )
            return
        except Exception as exc:
            # Keep unexpected failures visible in the deployment thread without
            # dumping a traceback or credentials into a public channel.
            target = thread or await ensure_thread()
            await self._send(target, f"Deployment failed for `{app_name}`: {type(exc).__name__}: {exc}")
            return

        target = thread or await ensure_thread()
        summary = (
            f"`{app_name}` already runs `{result.sha}`; no deployment was needed."
            if result.status == "noop"
            else f"Deployment complete for `{app_name}` at `{result.sha}`."
        )
        await self._send_with_transcript(
            target,
            summary,
            app_name,
            result.transcript,
        )

    async def _send_with_transcript(
        self,
        target: Any,
        content: str,
        app_name: str,
        transcript: bytes,
    ) -> None:
        parts = attachment_parts(transcript, f"botman-{app_name}-deployment.log")
        kwargs: dict[str, Any] = {}
        try:
            kwargs["files"] = discord_files(parts)
        except ImportError:
            pass
        await self._send(target, content, **kwargs)

    @staticmethod
    async def _send(target: Any, content: str, **kwargs: Any) -> None:
        try:
            import discord  # type: ignore[import-not-found]

            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        except ImportError:
            pass
        await target.send(content, **kwargs)


def register_update_command(bot: Any, adapter: UpdateCommandAdapter) -> None:
    async def update(ctx: Any, app: str) -> None:
        await adapter.invoke(ctx, app)

    update.__name__ = "botman_update"
    bot.hybrid_command(
        name="update",
        description="Fetch and deploy one managed app from its configured Git branch.",
    )(update)
