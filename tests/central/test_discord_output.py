from botman.discord_output import command_transcript, present_exec_result
from botman.executor import ExecResult


def test_short_command_output_is_inline_and_code_fences_are_neutralized() -> None:
    result = ExecResult(("docker", "compose", "ps"), 0, "hello ``` world", "")
    shown = present_exec_result("status", "app-a", result)
    assert shown.attachment_bytes is None
    assert "``` world" not in shown.content
    assert len(shown.content) <= 1900


def test_long_command_output_gets_complete_attachment() -> None:
    stdout = "x" * 5000
    result = ExecResult(("docker", "compose", "build", "svc"), 1, stdout, "boom")
    shown = present_exec_result("restart", "app-a", result)
    assert shown.attachment_name == "botman-app-a-restart.log"
    assert shown.attachment_bytes == command_transcript(result)
    assert stdout.encode() in shown.attachment_bytes
    assert len(shown.content) <= 1900


def test_attachment_parts_split_large_transcript_with_valid_budgets() -> None:
    from botman.discord_output import attachment_parts

    data = (b"line-0123456789\n" * 2000)
    parts = attachment_parts(data, "deploy.log", max_bytes=2048)
    assert len(parts) > 1
    assert all(name.endswith(".log.gz") for name, _ in parts)
    assert all(len(chunk) <= 2048 for _, chunk in parts)
