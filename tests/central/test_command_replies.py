from botman.commands.replies import DISCORD_MESSAGE_LIMIT, file_preview_message


def test_short_content_is_inlined_in_code_block() -> None:
    message = file_preview_message("Header.", "A=1\nB=2\n", "ini")
    assert message == "Header.\n```ini\nA=1\nB=2\n```"


def test_content_too_long_falls_back_to_header() -> None:
    content = "x" * DISCORD_MESSAGE_LIMIT
    assert file_preview_message("Header.", content, "yaml") == "Header."


def test_content_at_limit_is_inlined() -> None:
    overhead = len("Header.\n```yaml\n\n```")
    content = "x" * (DISCORD_MESSAGE_LIMIT - overhead)
    message = file_preview_message("Header.", content, "yaml")
    assert len(message) == DISCORD_MESSAGE_LIMIT
    assert content in message


def test_code_fence_in_content_falls_back_to_header() -> None:
    assert file_preview_message("Header.", "a: ```\n", "yaml") == "Header."
