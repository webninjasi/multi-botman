"""Discord preview/confirm flow for `/config app remove`."""

from __future__ import annotations

from typing import Any

from ..app_removal import AppRemovalPlan, AppRemovalService
from .compose_apply import send_exec_reply
from .confirm import Authorize, confirm_view, truncate_message


def render_removal_plan(plan: AppRemovalPlan) -> str:
    lines = [f"**Remove app `{plan.app_name}` from stack `{plan.stack_name}`** (cannot be undone)"]
    if plan.containers:
        lines.append(f"Containers of `{plan.service}`: {len(plan.containers)}")
    lines.append("Built image, if present: " + " or ".join(f"`{image}`" for image in plan.images))
    lines.append("Target files: " + ", ".join(f"`{path}`" for path in plan.target_paths))
    if plan.central_paths:
        lines.append("Central files: " + ", ".join(f"`{path}`" for path in plan.central_paths))
    if plan.live_logs:
        lines.append("Live logs: subscription removed and the log agent re-synced")
    if plan.webhook_id:
        lines.append("Discord log webhook")
    lines.append("Config entry")
    for item in plan.kept:
        lines.append(f"Kept: {item}")
    lines.append("Volumes are never deleted.")
    return truncate_message("\n".join(lines))


async def _delete_webhook(channel: Any, webhook_id: str) -> str:
    """Best-effort: the app's log webhook is a credential nothing uses any more."""

    try:
        for webhook in await channel.webhooks():
            if str(webhook.id) == webhook_id:
                await webhook.delete(reason="Botman app removed")
                return "Discord log webhook deleted."
        return "Discord log webhook was already gone."
    except Exception as exc:
        return f"Could not delete the Discord log webhook ({type(exc).__name__}); delete it in channel settings."


async def send_removal_preview(
    interaction: Any,
    service: AppRemovalService,
    app_name: str,
    *,
    authorize: Authorize,
) -> None:
    """Send the removal plan with Remove/Cancel buttons to an already-deferred interaction."""

    channel_id = interaction.channel_id
    plan = await service.plan(app_name=app_name, channel_id=channel_id)
    text = render_removal_plan(plan)

    async def run(button_interaction: Any) -> None:
        result = await service.remove(app_name=app_name, channel_id=channel_id, expected=plan)
        await send_exec_reply(button_interaction, "app remove", app_name, result)
        if result.ok and plan.webhook_id and button_interaction.channel is not None:
            note = await _delete_webhook(button_interaction.channel, plan.webhook_id)
            await button_interaction.followup.send(note, ephemeral=True)

    view = confirm_view(text, label="Remove", progress="Removing…", run=run, authorize=authorize)
    await interaction.followup.send(text, view=view, ephemeral=True)
