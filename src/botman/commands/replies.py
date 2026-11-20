"""Shared reply shapes for Discord command adapters."""

from __future__ import annotations

import io
from typing import Any

DISCORD_MESSAGE_LIMIT = 2000


def file_preview_message(header: str, content: str, language: str = "") -> str:
    """Return `header` plus `content` in a code block when it fits one message.

    Falls back to the bare header when the content is too long or contains a
    code fence that would break the block.
    """
    if "```" in content:
        return header
    body = content.rstrip("\n")
    message = f"{header}\n```{language}\n{body}\n```"
    if len(message) > DISCORD_MESSAGE_LIMIT:
        return header
    return message


async def send_file_preview(
    interaction: Any,
    *,
    header: str,
    content: str,
    filename: str,
    language: str = "",
) -> None:
    """Send `content` inline when it fits, always with the file attached."""
    import discord

    attachment = discord.File(io.BytesIO(content.encode()), filename=filename)
    await interaction.followup.send(
        file_preview_message(header, content, language),
        file=attachment,
        ephemeral=True,
    )
