"""Stack-aware app lookup and Discord command-channel authorization."""

from __future__ import annotations

from dataclasses import dataclass

from .models import AppConfig, BotmanConfig, ServerConfig, StackConfig


class UnknownAppError(KeyError):
    """Raised when a command references an unknown app within a stack."""


class ChannelAuthorizationError(PermissionError):
    """Raised when a command is invoked outside every configured stack channel."""


@dataclass(frozen=True, slots=True)
class ResolvedApp:
    name: str
    app: AppConfig
    stack_name: str
    stack: StackConfig
    server_name: str
    server: ServerConfig

    @property
    def qualified_name(self) -> str:
        return f"{self.stack_name}.{self.name}"


def resolve_app(config: BotmanConfig, stack_name: str, app_name: str) -> ResolvedApp:
    try:
        stack = config.stacks[stack_name]
    except KeyError as exc:
        raise KeyError(f"unknown stack: {stack_name}") from exc
    try:
        app = stack.apps[app_name]
    except KeyError as exc:
        raise UnknownAppError(
            f"unknown app {app_name!r} in stack {stack_name!r}"
        ) from exc
    server = config.servers[stack.server]
    return ResolvedApp(
        name=app_name,
        app=app,
        stack_name=stack_name,
        stack=stack,
        server_name=stack.server,
        server=server,
    )


def resolve_stack_channel(
    config: BotmanConfig, invocation_channel_id: str | int
) -> tuple[str, StackConfig]:
    try:
        return config.stack_for_channel(invocation_channel_id)
    except KeyError as exc:
        raise ChannelAuthorizationError(
            "this channel is not configured as a Botman stack command channel"
        ) from exc


def authorize_app_channel(
    config: BotmanConfig, app_name: str, invocation_channel_id: str | int
) -> ResolvedApp:
    """Resolve the stack from the channel, then resolve the app inside that stack.

    App names are intentionally only unique within their stack. A command never
    searches other stacks for a matching app name.
    """

    stack_name, _ = resolve_stack_channel(config, invocation_channel_id)
    return resolve_app(config, stack_name, app_name)
