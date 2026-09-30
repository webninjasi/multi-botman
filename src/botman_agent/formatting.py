"""Discord-safe live-log rendering with line-aware splitting."""

from __future__ import annotations

from dataclasses import dataclass

CODE_OPEN = "```text\n"
CODE_CLOSE = "\n```"
DEFAULT_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class RenderedSegment:
    content: str
    final_for_entry: bool


def sanitize_markdown(text: str) -> str:
    """Prevent a journal message from terminating our code fence."""
    return text.replace("```", "``\u200b`")


def _wrap(payload: str) -> str:
    return f"{CODE_OPEN}{payload}{CODE_CLOSE}"


def _payload_budget(limit: int) -> int:
    budget = limit - len(CODE_OPEN) - len(CODE_CLOSE)
    if budget < 1:
        raise ValueError("Discord message limit is too small for log wrapper")
    return budget


def _truncate_huge_line(line: str, *, original_length: int, budget: int) -> str:
    marker = (
        f" … [line truncated for Discord; original length={original_length} chars; "
        "use /logs download]"
    )
    if len(marker) >= budget:
        marker = " … [line truncated]"
    keep = max(0, budget - len(marker))
    return line[:keep] + marker


def render_entry(message: str, *, limit: int = DEFAULT_LIMIT) -> list[RenderedSegment]:
    """Render one journal MESSAGE into <=limit Discord payloads.

    Whole logical lines are kept intact whenever they fit. Only an individual
    logical line which cannot fit by itself is truncated. The final segment is
    explicitly marked so callers can advance the journal cursor only after it
    has been acknowledged.
    """
    budget = _payload_budget(limit)
    logical_lines = message.split("\n")
    if not logical_lines:
        logical_lines = [""]

    payloads: list[str] = []
    pending: list[str] = []
    pending_len = 0

    def flush() -> None:
        nonlocal pending, pending_len
        if pending:
            payloads.append("\n".join(pending))
            pending = []
            pending_len = 0

    for raw_line in logical_lines:
        line = sanitize_markdown(raw_line)
        if len(line) > budget:
            flush()
            payloads.append(
                _truncate_huge_line(line, original_length=len(raw_line), budget=budget)
            )
            continue
        candidate_len = len(line) if not pending else pending_len + 1 + len(line)
        if candidate_len > budget:
            flush()
        pending.append(line)
        pending_len = len(line) if pending_len == 0 else pending_len + 1 + len(line)
    flush()

    if not payloads:
        payloads = [""]
    rendered = [_wrap(payload) for payload in payloads]
    if any(len(content) > limit for content in rendered):  # defensive invariant
        raise AssertionError("formatter produced an oversized Discord payload")
    return [
        RenderedSegment(content=content, final_for_entry=index == len(rendered) - 1)
        for index, content in enumerate(rendered)
    ]


def gap_marker(*, limit: int = DEFAULT_LIMIT) -> str:
    content = _wrap(
        "[Botman] Live-log checkpoint was too old to resume safely; continuing from the live tail. "
        "Use /logs download for the skipped interval."
    )
    if len(content) > limit:
        raise AssertionError("gap marker exceeds Discord limit")
    return content
