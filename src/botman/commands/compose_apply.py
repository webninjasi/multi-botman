"""Discord preview/confirm flow for `/config compose apply`.

`discord` is imported only when a view is built, so command registration stays
importable without it (see the admin registration tests).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..compose import ComposeAdminService, ComposeApplyPlan
from ..discord_output import bytes_file, present_exec_result

Authorize = Callable[[Any], Awaitable[bool]]
PREVIEW_TIMEOUT_SEC = 300
MESSAGE_LIMIT = 2000


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
    text = "\n".join(lines)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 2] + " …"
    return text


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
    view = _confirm_view(service, plan, text, authorize=authorize)
    await interaction.followup.send(text, view=view, ephemeral=True)


async def _run_apply(
    interaction: Any, service: ComposeAdminService, plan: ComposeApplyPlan
) -> None:
    try:
        result = await service.apply(plan.stack_name, plan)
    except Exception as exc:
        await interaction.followup.send(f"Failed: {exc}", ephemeral=True)
        return
    presentation = present_exec_result("compose apply", plan.stack_name, result)
    kwargs: dict[str, Any] = {}
    if presentation.attachment_bytes is not None and presentation.attachment_name is not None:
        kwargs["file"] = bytes_file(presentation.attachment_bytes, presentation.attachment_name)
    await interaction.followup.send(presentation.content, ephemeral=True, **kwargs)


def _confirm_view(
    service: ComposeAdminService,
    plan: ComposeApplyPlan,
    preview: str,
    *,
    authorize: Authorize,
) -> Any:
    import discord

    class ApplyConfirmView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=PREVIEW_TIMEOUT_SEC)

        @discord.ui.button(label="Apply", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            if not await authorize(interaction):
                return
            self.stop()
            await interaction.response.edit_message(content=f"{preview}\n\nApplying…", view=None)
            await _run_apply(interaction, service, plan)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
            self.stop()
            await interaction.response.edit_message(content=f"{preview}\n\nCancelled.", view=None)

    return ApplyConfirmView()


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
