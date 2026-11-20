"""Confirm/Cancel buttons under an ephemeral preview.

`discord` is imported only when a view is built, so command registration stays
importable without it (see the admin registration tests).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Authorize = Callable[[Any], Awaitable[bool]]
Run = Callable[[Any], Awaitable[None]]
PREVIEW_TIMEOUT_SEC = 300


def confirm_view(preview: str, *, label: str, progress: str, run: Run, authorize: Authorize) -> Any:
    """Buttons that re-check admin access, then edit the preview and call `run`.

    `run` receives the button interaction, whose response is already used, so
    it replies with `interaction.followup`.
    """

    import discord

    class ConfirmView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=PREVIEW_TIMEOUT_SEC)

        @discord.ui.button(label=label, style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            if not await authorize(interaction):
                return
            self.stop()
            await interaction.response.edit_message(content=f"{preview}\n\n{progress}", view=None)
            try:
                await run(interaction)
            except Exception as exc:
                await interaction.followup.send(f"Failed: {exc}", ephemeral=True)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            self.stop()
            await interaction.response.edit_message(content=f"{preview}\n\nCancelled.", view=None)

    return ConfirmView()


def truncate_message(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[: limit - 2] + " …"
