"""Slash-only, ephemeral environment management for stack-authorized deployers."""

from __future__ import annotations

import io

from ..compose import StackLockRegistry
from ..config import ConfigStore
from ..env import ENV_UPLOAD_MAX_BYTES, EnvMissingError, EnvService


def register_env_commands(
    bot,
    store: ConfigStore,
    *,
    locks: StackLockRegistry,
) -> None:
    """Register `/env ...` commands using stack-channel authorization.

    Environment commands deliberately use the same authorization boundary as
    `/update`: the current Discord channel identifies the stack, and the app
    must exist inside that stack. They are not gated by `ADMIN_IDS`.
    """

    import discord  # type: ignore[import-not-found]
    from discord import app_commands  # type: ignore[import-not-found]

    async def begin(interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

    async def fail(interaction, exc: Exception) -> None:
        await interaction.followup.send(f"Failed: {exc}", ephemeral=True)

    env_group = app_commands.Group(name="env", description="Manage app environment files.")

    @env_group.command(name="show", description="Show an app's current environment file.")
    async def env_show(interaction: discord.Interaction, app: str):
        await begin(interaction)
        try:
            if interaction.channel_id is None:
                raise ValueError("env commands must be run in a configured stack channel")
            env_file = await EnvService(store, locks=locks).show(app, interaction.channel_id)
            attachment = discord.File(
                io.BytesIO(env_file.content.encode()), filename=f"{app}.env"
            )
            await interaction.followup.send(
                f"Environment file for `{app}` (`{env_file.path}`).",
                file=attachment,
                ephemeral=True,
            )
        except EnvMissingError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await fail(interaction, exc)

    @env_group.command(name="upload", description="Replace an app's environment file.")
    async def env_upload(
        interaction: discord.Interaction,
        app: str,
        file: discord.Attachment,
    ):
        await begin(interaction)
        try:
            if file.size > ENV_UPLOAD_MAX_BYTES:
                raise ValueError(
                    f"environment attachment exceeds {ENV_UPLOAD_MAX_BYTES}-byte limit"
                )
            if interaction.channel_id is None:
                raise ValueError("env commands must be run in a configured stack channel")
            path = await EnvService(store, locks=locks).upload(
                app, interaction.channel_id, await file.read()
            )
            await interaction.followup.send(
                f"Environment for `{app}` written to `{path}`. No restart/deploy was performed.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @env_group.command(name="set", description="Set or replace one environment variable.")
    async def env_set(
        interaction: discord.Interaction,
        app: str,
        key: str,
        value: str,
    ):
        await begin(interaction)
        try:
            if interaction.channel_id is None:
                raise ValueError("env commands must be run in a configured stack channel")
            await EnvService(store, locks=locks).set(app, interaction.channel_id, key, value)
            await interaction.followup.send(
                f"`{key}` updated for `{app}`. No restart/deploy was performed.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @env_group.command(name="unset", description="Remove one environment variable.")
    async def env_unset(interaction: discord.Interaction, app: str, key: str):
        await begin(interaction)
        try:
            if interaction.channel_id is None:
                raise ValueError("env commands must be run in a configured stack channel")
            await EnvService(store, locks=locks).unset(app, interaction.channel_id, key)
            await interaction.followup.send(
                f"`{key}` removed from `{app}`. No restart/deploy was performed.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    bot.tree.add_command(env_group)
