from __future__ import annotations

import pytest

from botman.commands.errors import on_app_command_error, on_command_error


class Wrapped(Exception):
    def __init__(self, original: BaseException) -> None:
        super().__init__("wrapped")
        self.original = original


class FakeContext:
    def __init__(self, interaction=None) -> None:
        self.interaction = interaction
        self.command = "update"
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


class FakeResponse:
    def __init__(self, done: bool) -> None:
        self.done = done
        self.sent = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeFollowup:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeInteraction:
    def __init__(self, done: bool) -> None:
        self.command = "env"
        self.response = FakeResponse(done)
        self.followup = FakeFollowup()


@pytest.mark.asyncio
async def test_command_error_replies_with_type_only() -> None:
    ctx = FakeContext()
    await on_command_error(ctx, Wrapped(OSError("ssh root@10.0.0.1 secret-path")))
    assert len(ctx.sent) == 1
    assert "OSError" in ctx.sent[0]
    assert "secret-path" not in ctx.sent[0]


@pytest.mark.asyncio
async def test_app_command_error_uses_followup_after_defer() -> None:
    interaction = FakeInteraction(done=True)
    await on_app_command_error(interaction, Wrapped(RuntimeError("boom")))
    assert interaction.followup.sent[0][1]["ephemeral"] is True
    assert interaction.response.sent == []


@pytest.mark.asyncio
async def test_app_command_error_uses_initial_response_when_not_deferred() -> None:
    interaction = FakeInteraction(done=False)
    await on_app_command_error(interaction, RuntimeError("boom"))
    assert len(interaction.response.sent) == 1


@pytest.mark.asyncio
async def test_hybrid_error_is_reported_once_across_both_handlers() -> None:
    interaction = FakeInteraction(done=True)
    ctx = FakeContext(interaction)
    await on_command_error(ctx, RuntimeError("boom"))
    await on_app_command_error(interaction, RuntimeError("boom"))
    assert len(ctx.sent) == 1
    assert interaction.followup.sent == []
