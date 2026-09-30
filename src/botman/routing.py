"""Stack-aware app lookup and Discord command-channel authorization."""

from __future__ import annotations

from dataclasses import dataclass

from .models import AppConfig, BotmanConfig, ServerConfig, StackConfig


class UnknownAppError(KeyError):
    """Raised when a command references an unknown app."""


class ChannelAuthorizationError(PermissionError):
    """Raised when an app command is invoked outside its stack command channel."""


@dataclass(frozen=True, slots=True)
class ResolvedApp:
    name: str
    app: AppConfig
    stack_name: str
    stack: StackConfig
    server_name: str
    server: ServerConfig


def resolve_app(config: BotmanConfig, app_name: str) -> ResolvedApp:
    try:
        app = config.apps[app_name]
    except KeyError as exc:
        raise UnknownAppError(f"unknown app: {app_name}") from exc
    stack = config.stacks[app.stack]
    server = config.servers[stack.server]
    return ResolvedApp(
        name=app_name,
        app=app,
        stack_name=app.stack,
        stack=stack,
        server_name=stack.server,
        server=server,
    )


def authorize_app_channel(
    config: BotmanConfig, app_name: str, invocation_channel_id: str | int
) -> ResolvedApp:
    resolved = resolve_app(config, app_name)
    actual = str(invocation_channel_id)
    expected = resolved.stack.channel_id
    if actual != expected:
        raise ChannelAuthorizationError(
            f"app {app_name!r} belongs to stack {resolved.stack_name!r} and may only be managed "
            f"from channel {expected}"
        )
    return resolved
