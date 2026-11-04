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
        starter = None

        async def ensure_thread():
            # The thread hangs off the command's own reply, which also answers
            # the deferred interaction. Created lazily so rejected requests
            # (wrong channel/unknown app) get a plain reply and no thread.
            nonlocal thread, starter
            if thread is not None:
                return thread
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            name = f"deploy-{app_name}-{timestamp}"[:100]
            if starter is None:
                starter = await self._send(ctx, f"Deploying `{app_name}`; progress follows in the thread.")
            create_from_message = getattr(starter, "create_thread", None)
            if callable(create_from_message):
                try:
                    thread = await create_from_message(name=name)
                    return thread
                except Exception:
                    pass
            create_thread = getattr(channel, "create_thread", None)
            if not callable(create_thread):
                raise RuntimeError("this channel does not support deployment threads")
            try:
                import discord

                thread = await create_thread(name=name, type=discord.ChannelType.public_thread)
            except ImportError:
                thread = await create_thread(name=name)
            return thread

        async def failure_target():
            if thread is not None:
                return thread
            try:
                return await ensure_thread()
            except Exception:
                return ctx

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
            target = await failure_target()
            transcript = (
                exc.transcript.render()
                if isinstance(exc, DeploymentError)
                else (f"Git failure: {exc}\n").encode()
            )
            message = f"Deployment failed for `{app_name}`: {exc}"
            await self._send_with_transcript(target, message, app_name, transcript)
            await self._edit(starter, message)
            return
        except Exception as exc:
            # Keep unexpected failures visible in the deployment thread without
            # dumping a traceback or credentials into a public channel.
            target = await failure_target()
            message = f"Deployment failed for `{app_name}`: {type(exc).__name__}: {exc}"
            await self._send(target, message)
            await self._edit(starter, message)
            return

        target = await failure_target()
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
        await self._edit(starter, summary)

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
    async def _send(target: Any, content: str, **kwargs: Any) -> Any:
        try:
            import discord

            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        except ImportError:
            pass
        return await target.send(content, **kwargs)

    @staticmethod
    async def _edit(message: Any, content: str) -> None:
        """Best-effort: mirror the final status on the thread's starter reply."""
        edit = getattr(message, "edit", None)
        if not callable(edit):
            return
        try:
            await edit(content=content[:2000])
        except Exception:
            pass


def register_update_command(bot: Any, adapter: UpdateCommandAdapter) -> None:
    async def update(ctx: Any, app: str) -> None:
        await adapter.invoke(ctx, app)

    update.__name__ = "botman_update"
    bot.hybrid_command(
        name="update",
        description="Fetch and deploy one managed app from its configured Git branch.",
    )(update)
