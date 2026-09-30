"""Discord-safe command output and transcript helpers."""

from __future__ import annotations

import io
import shlex
from dataclasses import dataclass

from .executor import ExecResult

DISCORD_CONTENT_LIMIT = 2000
DEFAULT_MESSAGE_BUDGET = 1900


def _neutralize_code_fences(text: str) -> str:
    """Prevent command output from closing a Markdown code block."""

    return text.replace("```", "``\u200b`")


def _truncate(text: str, limit: int, *, marker: str = "\n… truncated …") -> str:
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return marker[:limit]
    return text[: limit - len(marker)] + marker


def command_transcript(result: ExecResult) -> bytes:
    """Build a complete plain-text transcript for attachment or audit use."""

    parts = [
        f"$ {shlex.join(result.argv)}",
        f"exit: {result.returncode}",
        "",
        "--- stdout ---",
        result.stdout.rstrip("\n"),
        "",
        "--- stderr ---",
        result.stderr.rstrip("\n"),
        "",
    ]
    return "\n".join(parts).encode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class CommandPresentation:
    content: str
    attachment_name: str | None = None
    attachment_bytes: bytes | None = None


def present_exec_result(
    operation: str,
    app_name: str,
    result: ExecResult,
    *,
    budget: int = DEFAULT_MESSAGE_BUDGET,
) -> CommandPresentation:
    """Render concise lifecycle output and retain full output when it is long."""

    if budget > DISCORD_CONTENT_LIMIT or budget < 200:
        raise ValueError("budget must be between 200 and Discord's 2000-character limit")

    status = "ok" if result.ok else f"failed (exit {result.returncode})"
    header = f"**{operation} `{app_name}`** — {status}"
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    body_parts: list[str] = []
    if stdout:
        body_parts.append(stdout)
    if stderr:
        body_parts.append(f"stderr:\n{stderr}")
    body = _neutralize_code_fences("\n".join(body_parts) or "(no output)")
    full = f"{header}\n```text\n{body}\n```"
    if len(full) <= budget:
        return CommandPresentation(content=full)

    transcript = command_transcript(result)
    suffix = "\nFull command output is attached."
    available = budget - len(header) - len(suffix) - len("\n```text\n\n```")
    preview = _truncate(body, max(32, available))
    content = f"{header}\n```text\n{preview}\n```{suffix}"
    if len(content) > budget:
        content = _truncate(content, budget)
    safe_app = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in app_name)
    return CommandPresentation(
        content=content,
        attachment_name=f"botman-{safe_app}-{operation}.log",
        attachment_bytes=transcript,
    )


def bytes_file(data: bytes, filename: str):
    """Create a discord.File lazily so formatter unit tests need no Discord import."""

    import discord  # type: ignore[import-not-found]

    return discord.File(io.BytesIO(data), filename=filename)


def attachment_parts(
    data: bytes,
    filename: str,
    *,
    max_bytes: int = 7_500_000,
) -> list[tuple[str, bytes]]:
    """Split a large text transcript into independently valid gzip attachments."""

    if max_bytes < 1024:
        raise ValueError("max_bytes is unreasonably small")
    if len(data) <= max_bytes:
        return [(filename, data)]

    import gzip

    # Use smaller raw chunks so gzip framing overhead cannot push ordinary
    # transcript parts beyond the requested attachment budget. If a compressed
    # part still exceeds it (pathological incompressible data), bisect it.
    raw_budget = max(1024, max_bytes - 4096)
    chunks: list[bytes] = []
    remaining = data
    while remaining:
        if len(remaining) <= raw_budget:
            chunks.append(remaining)
            break
        cut = remaining.rfind(b"\n", 0, raw_budget + 1)
        if cut <= 0:
            cut = raw_budget
        else:
            cut += 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]

    def compress_fit(chunk: bytes) -> list[bytes]:
        packed = gzip.compress(chunk, compresslevel=6, mtime=0)
        if len(packed) <= max_bytes:
            return [packed]
        if len(chunk) <= 1:
            raise ValueError("attachment budget too small for gzip framing")
        midpoint = len(chunk) // 2
        return compress_fit(chunk[:midpoint]) + compress_fit(chunk[midpoint:])

    packed_chunks: list[bytes] = []
    for chunk in chunks:
        packed_chunks.extend(compress_fit(chunk))

    stem = filename[:-4] if filename.endswith(".log") else filename
    width = max(2, len(str(len(packed_chunks))))
    return [
        (f"{stem}.part{index:0{width}d}.log.gz", chunk)
        for index, chunk in enumerate(packed_chunks, start=1)
    ]


def discord_files(parts: list[tuple[str, bytes]]):
    """Create discord.File objects lazily for a list of attachment parts."""

    return [bytes_file(data, name) for name, data in parts]
