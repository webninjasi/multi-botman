"""Discord preview/confirm flows for `/config compose apply` and `/config compose prune`.

`discord` is imported only when a view is built, so command registration stays
importable without it (see the admin registration tests).
"""

from __future__ import annotations

from typing import Any

from ..compose import ComposeAdminService, ComposeApplyPlan, ComposePrunePlan
from ..discord_output import bytes_file, present_exec_result
from .confirm import PREVIEW_TIMEOUT_SEC, Authorize, confirm_view, truncate_message


def _names(services: tuple[str, ...]) -> str:
    return ", ".join(f"`{service}`" for service in services)


def render_apply_plan(plan: ComposeApplyPlan) -> str:
    lines = [f"**Compose apply for `{plan.stack_name}`**"]
    if plan.orphans:
        lines.append(f"Remove containers of services no longer in the file: {_names(plan.orphans)}")
    if plan.unchanged is None:
        if plan.services:
            lines.append(
                f"`up -d` (creates new services, recreates changed ones, starts stopped ones): "
                f"{_names(plan.services)}"
            )
    else:
        if plan.changed:
            lines.append(f"Create or recreate (new or changed config): {_names(plan.changed)}")
        if plan.unchanged:
            lines.append(f"Keep as is, start if stopped: {_names(plan.unchanged)}")
    for service, reason in plan.skipped:
        lines.append(f"Skip `{service}`: {reason}")
    if plan.empty:
        lines.append("Nothing to apply.")
    else:
        lines.append("Volumes and networks are never deleted.")
    return truncate_message("\n".join(lines))


async def send_exec_reply(interaction: Any, operation: str, subject: str, result) -> None:
    """Follow up with command output, attaching it when it is long."""

    presentation = present_exec_result(operation, subject, result)
    kwargs: dict[str, Any] = {}
    if presentation.attachment_bytes is not None and presentation.attachment_name is not None:
        kwargs["file"] = bytes_file(presentation.attachment_bytes, presentation.attachment_name)
    await interaction.followup.send(presentation.content, ephemeral=True, **kwargs)


async def send_apply_preview(
    interaction: Any,
    service: ComposeAdminService,
    stack_name: str,
    *,
    authorize: Authorize,
) -> None:
    """Send the plan with Apply/Cancel buttons to an already-deferred interaction."""

    plan = await service.plan_apply(stack_name)
    text = render_apply_plan(plan)
    if plan.empty:
        await interaction.followup.send(text, ephemeral=True)
        return

    async def run(button_interaction: Any) -> None:
        result = await service.apply(plan.stack_name, plan)
        await send_exec_reply(button_interaction, "compose apply", plan.stack_name, result)

    view = confirm_view(text, label="Apply", progress="Applying…", run=run, authorize=authorize)
    await interaction.followup.send(text, view=view, ephemeral=True)


def apply_now_view(
    service: ComposeAdminService, stack_name: str, *, authorize: Authorize
) -> Any:
    """A button under the upload reply that opens the same apply preview."""

    import discord

    class ApplyNowView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=PREVIEW_TIMEOUT_SEC)

        @discord.ui.button(label="Apply now", style=discord.ButtonStyle.primary)
        async def apply_now(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            if not await authorize(interaction):
                return
            self.stop()
            await interaction.response.edit_message(view=None)
            try:
                await send_apply_preview(interaction, service, stack_name, authorize=authorize)
            except Exception as exc:
                await interaction.followup.send(f"Failed: {exc}", ephemeral=True)

    return ApplyNowView()


def render_prune_plan(plan: ComposePrunePlan) -> str:
    lines = [f"**Compose prune for `{plan.stack_name}`**"]
    if plan.networks:
        lines.append(f"Remove networks no longer in the file: {_names(plan.networks)}")
    if plan.volumes:
        if plan.include_volumes:
            lines.append(
                f"Remove volumes no longer in the file, **deleting their data**: {_names(plan.volumes)}"
            )
        else:
            lines.append(
                f"Volumes no longer in the file (kept; run with `volumes: True` to delete them "
                f"and their data): {_names(plan.volumes)}"
            )
    if plan.empty:
        lines.append("Nothing to prune.")
    else:
        lines.append("Anything a container still uses is refused by the runtime and kept.")
    return truncate_message("\n".join(lines))


async def send_prune_preview(
    interaction: Any,
    service: ComposeAdminService,
    stack_name: str,
    *,
    include_volumes: bool,
    authorize: Authorize,
) -> None:
    """Send the prune plan with Prune/Cancel buttons to an already-deferred interaction."""

    plan = await service.plan_prune(stack_name, include_volumes=include_volumes)
    text = render_prune_plan(plan)
    if plan.empty:
        await interaction.followup.send(text, ephemeral=True)
        return

    async def run(button_interaction: Any) -> None:
        result = await service.prune(plan.stack_name, plan)
        await send_exec_reply(button_interaction, "compose prune", plan.stack_name, result)

    view = confirm_view(text, label="Prune", progress="Pruning…", run=run, authorize=authorize)
    await interaction.followup.send(text, view=view, ephemeral=True)
