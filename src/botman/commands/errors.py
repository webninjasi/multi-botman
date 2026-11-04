"""Last-resort replies for command errors the adapters did not handle.

Without these, an unhandled exception after `defer()` leaves the slash
interaction "thinking" until Discord shows "The application did not respond".
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("botman.commands")

_REPORTED_ATTR = "_botman_error_reported"


def _unwrap(error: BaseException) -> BaseException:
    # Hybrid/app command errors nest the real exception under `.original`.
    seen = 0
    while getattr(error, "original", None) is not None and seen < 5:
        error = error.original  # type: ignore[attr-defined]
        seen += 1
    return error


def error_message(error: BaseException) -> str:
    try:
        from discord.ext import commands

        if isinstance(error, commands.UserInputError):
            return f"Invalid command usage: {error}"
        if isinstance(error, commands.CheckFailure):
            return "You are not allowed to run this command here."
    except ImportError:
        pass
    original = _unwrap(error)
    # Type name only: exception text may carry hosts, paths, or credentials.
    return f"Command failed unexpectedly ({type(original).__name__}). Check the bot logs."


def _mark_reported(interaction: Any) -> bool:
    """Return True if this interaction was already answered by a handler."""
    if interaction is None:
        return False
    if getattr(interaction, _REPORTED_ATTR, False):
        return True
    try:
        setattr(interaction, _REPORTED_ATTR, True)
    except Exception:
        pass
    return False


async def on_command_error(ctx: Any, error: BaseException) -> None:
    try:
        from discord.ext import commands

        if isinstance(error, commands.CommandNotFound):
            return
    except ImportError:
        pass
    log.error("command %s failed", getattr(ctx, "command", None), exc_info=_unwrap(error))
    if _mark_reported(getattr(ctx, "interaction", None)):
        return
    try:
        await ctx.send(error_message(error))
    except Exception:
        log.exception("could not report command error")


async def on_app_command_error(interaction: Any, error: BaseException) -> None:
    log.error("app command %s failed", getattr(interaction, "command", None), exc_info=_unwrap(error))
    if _mark_reported(interaction):
        return
    try:
        message = error_message(error)
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        log.exception("could not report app command error")
