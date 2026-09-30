"""Slash-only, ephemeral administrator commands for onboarding/configuration."""

import io
from typing import Literal

from ..admin import parse_admin_ids
from ..agent_control import AgentControlService
from ..admin_config import AdminConfigService, AdminGuard, parse_compose_argv
from ..compose import (
    MAX_COMPOSE_BYTES,
    ComposeAdminService,
    StackLockRegistry,
)
from ..config import ConfigStore
from ..env import ENV_UPLOAD_MAX_BYTES, EnvMissingError, EnvService
from ..git import GitAdminService


def register_admin_commands(bot, store: ConfigStore, *, admin_ids: str | None, locks: StackLockRegistry):
    """Register `/config ...` and `/env ...` application-command groups."""

    import discord  # type: ignore[import-not-found]
    from discord import app_commands  # type: ignore[import-not-found]

    guard = AdminGuard(parse_admin_ids(admin_ids))
    config_service = AdminConfigService(store)
    git_service = GitAdminService(store)

    async def authorize(interaction) -> bool:
        try:
            guard.require(interaction.user.id)
        except PermissionError:
            if interaction.response.is_done():
                await interaction.followup.send("Administrator access required.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "Administrator access required.", ephemeral=True
                )
            return False
        return True

    async def begin(interaction) -> bool:
        if not await authorize(interaction):
            return False
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        return True

    async def fail(interaction, exc: Exception) -> None:
        await interaction.followup.send(f"Failed: {exc}", ephemeral=True)

    config_group = app_commands.Group(name="config", description="Configure Botman resources.")
    server_group = app_commands.Group(name="server", description="Configure managed servers.")
    stack_group = app_commands.Group(name="stack", description="Configure Compose stacks.")
    app_group = app_commands.Group(name="app", description="Configure managed applications.")
    compose_group = app_commands.Group(name="compose", description="Manage stack Compose YAML.")
    git_group = app_commands.Group(name="git", description="Manage per-app Git deploy keys.")
    agent_group = app_commands.Group(name="agent", description="Manage target live-log agents.")

    @server_group.command(name="add", description="Add a local or SSH-managed server.")
    async def server_add(
        interaction: discord.Interaction,
        name: str,
        server_type: Literal["local", "ssh"],
        compose_argv: str = "docker compose",
        host: str | None = None,
        port: int = 22,
        user: str | None = None,
        key: str | None = None,
        known_hosts: str | None = None,
    ):
        if not await begin(interaction):
            return
        try:
            argv = parse_compose_argv(compose_argv)
            await config_service.add_server(
                name=name,
                server_type=server_type,
                compose_argv=argv,
                host=host,
                port=port,
                user=user,
                key=key,
                known_hosts=known_hosts,
            )
            await interaction.followup.send(f"Server `{name}` added.", ephemeral=True)
        except Exception as exc:
            await fail(interaction, exc)

    @server_group.command(name="test", description="Test SSH/local access and Compose availability.")
    async def server_test(interaction: discord.Interaction, name: str):
        if not await begin(interaction):
            return
        try:
            result = await config_service.test_server(name)
            detail = (result.stdout or result.stderr or "(no output)").strip()
            if len(detail) > 1500:
                detail = detail[:1500] + "\n… truncated …"
            status = "passed" if result.ok else f"failed with exit {result.returncode}"
            await interaction.followup.send(
                f"Server `{name}` test {status}.\n```text\n{detail.replace('```', '``\u200b`')}\n```",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @stack_group.command(name="add", description="Add a stack using this channel as its control channel.")
    async def stack_add(
        interaction: discord.Interaction,
        name: str,
        server: str,
        project_name: str | None = None,
    ):
        if not await begin(interaction):
            return
        try:
            if interaction.channel_id is None:
                raise ValueError("stack creation must be run in a guild channel")
            await config_service.add_stack(
                name=name,
                server=server,
                channel_id=interaction.channel_id,
                project_name=project_name,
            )
            await interaction.followup.send(
                f"Stack `{name}` added; this channel is now its command channel.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @app_group.command(name="add", description="Add an independently deployed app to a stack.")
    async def app_add(
        interaction: discord.Interaction,
        name: str,
        stack: str,
        service: str,
        repo_url: str,
        branch: str = "main",
        log_identifier: str | None = None,
    ):
        if not await begin(interaction):
            return
        try:
            await config_service.add_app(
                name=name,
                stack=stack,
                service=service,
                repo_url=repo_url,
                branch=branch,
                log_identifier=log_identifier,
            )
            await interaction.followup.send(
                f"App `{name}` added. Run `/config git setup` before `/update`.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @compose_group.command(name="upload", description="Validate and install a stack Compose file.")
    async def compose_upload(
        interaction: discord.Interaction,
        stack: str,
        file: discord.Attachment,
    ):
        if not await begin(interaction):
            return
        try:
            if file.size > MAX_COMPOSE_BYTES:
                raise ValueError(
                    f"Compose attachment exceeds {MAX_COMPOSE_BYTES}-byte limit"
                )
            payload = await file.read()
            config = store.load()
            result = await ComposeAdminService(config, locks=locks).upload(stack, payload)
            normalized = result.stdout.strip()
            message = f"Compose configuration for `{stack}` validated and installed."
            if normalized:
                message += " Runtime `compose config` passed."
            await interaction.followup.send(message, ephemeral=True)
        except Exception as exc:
            await fail(interaction, exc)

    @compose_group.command(name="show", description="Show the currently installed stack Compose file.")
    async def compose_show(interaction: discord.Interaction, stack: str):
        if not await begin(interaction):
            return
        try:
            config = store.load()
            content = await ComposeAdminService(config, locks=locks).show(stack)
            if len(content) <= 1600 and "```" not in content:
                await interaction.followup.send(
                    f"```yaml\n{content}\n```", ephemeral=True
                )
            else:
                attachment = discord.File(
                    io.BytesIO(content.encode()), filename=f"{stack}-compose.yml"
                )
                await interaction.followup.send(
                    f"Compose configuration for `{stack}`.", file=attachment, ephemeral=True
                )
        except Exception as exc:
            await fail(interaction, exc)

    @git_group.command(name="setup", description="Generate the app's read-only Git deploy key.")
    async def git_setup(interaction: discord.Interaction, app: str):
        if not await begin(interaction):
            return
        try:
            public = await git_service.setup(app, replace=False)
            await interaction.followup.send(
                "Add this public key to the repository as a read-only deploy key, then run `/update`.\n"
                f"```text\n{public}\n```",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @git_group.command(name="rotate-key", description="Replace an app's Git deploy keypair.")
    async def git_rotate(interaction: discord.Interaction, app: str):
        if not await begin(interaction):
            return
        try:
            public = await git_service.setup(app, replace=True)
            await interaction.followup.send(
                "Deploy key rotated. Replace the repository's old deploy key with this public key "
                "before the next `/update`.\n"
                f"```text\n{public}\n```",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @agent_group.command(name="sync", description="Write current live-log config and restart a target agent.")
    async def agent_sync(interaction: discord.Interaction, server: str):
        if not await begin(interaction):
            return
        try:
            await AgentControlService(store.load()).sync(server)
            await interaction.followup.send(
                f"Log agent config synchronized and service active on `{server}`.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @agent_group.command(name="status", description="Show target Botman log-agent service status.")
    async def agent_status(interaction: discord.Interaction, server: str):
        if not await begin(interaction):
            return
        try:
            result = await AgentControlService(store.load()).status(server)
            detail = (result.stdout or result.stderr or "(no output)").strip()
            if len(detail) > 1600:
                detail = detail[:1600] + "\n… truncated …"
            await interaction.followup.send(
                f"Agent status on `{server}` (exit {result.returncode}):\n"
                f"```text\n{detail.replace('```', '``\u200b`')}\n```",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    config_group.add_command(server_group)
    config_group.add_command(stack_group)
    config_group.add_command(app_group)
    config_group.add_command(compose_group)
    config_group.add_command(git_group)
    config_group.add_command(agent_group)
    bot.tree.add_command(config_group)

    env_group = app_commands.Group(name="env", description="Manage app environment files.")

    @env_group.command(name="show", description="Show an app's current environment file.")
    async def env_show(interaction: discord.Interaction, app: str):
        if not await begin(interaction):
            return
        try:
            env_file = await EnvService(store.load()).show(app)
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
        if not await begin(interaction):
            return
        try:
            if file.size > ENV_UPLOAD_MAX_BYTES:
                raise ValueError(
                    f"environment attachment exceeds {ENV_UPLOAD_MAX_BYTES}-byte limit"
                )
            path = await EnvService(store.load()).upload(app, await file.read())
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
        if not await begin(interaction):
            return
        try:
            await EnvService(store.load()).set(app, key, value)
            await interaction.followup.send(
                f"`{key}` updated for `{app}`. No restart/deploy was performed.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    @env_group.command(name="unset", description="Remove one environment variable.")
    async def env_unset(interaction: discord.Interaction, app: str, key: str):
        if not await begin(interaction):
            return
        try:
            await EnvService(store.load()).unset(app, key)
            await interaction.followup.send(
                f"`{key}` removed from `{app}`. No restart/deploy was performed.",
                ephemeral=True,
            )
        except Exception as exc:
            await fail(interaction, exc)

    bot.tree.add_command(env_group)
