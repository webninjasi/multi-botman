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


def _entry_payloads(message: str, *, budget: int) -> list[str]:
    """Split one journal MESSAGE into sanitized, unwrapped <=budget payloads."""
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
    return payloads


def render_entry(message: str, *, limit: int = DEFAULT_LIMIT) -> list[RenderedSegment]:
    """Render one journal MESSAGE into <=limit Discord payloads.

    Whole logical lines are kept intact whenever they fit. Only an individual
    logical line which cannot fit by itself is truncated. The final segment is
    explicitly marked so callers can advance the journal cursor only after it
    has been acknowledged.
    """
    payloads = _entry_payloads(message, budget=_payload_budget(limit))
    rendered = [_wrap(payload) for payload in payloads]
    if any(len(content) > limit for content in rendered):  # defensive invariant
        raise AssertionError("formatter produced an oversized Discord payload")
    return [
        RenderedSegment(content=content, final_for_entry=index == len(rendered) - 1)
        for index, content in enumerate(rendered)
    ]


class LiveBatch:
    """Packs consecutive journal entries into as few <=limit Discord messages as possible.

    At most one message worth of text is held. A complete entry is appended to
    the pending batch when it fits; otherwise the pending batch is emitted first.
    An oversized multi-line entry emits all but its final segment immediately,
    and its final segment starts the next pending batch, so entry order is kept.
    """

    def __init__(self, *, limit: int = DEFAULT_LIMIT):
        self.limit = limit
        self._budget = _payload_budget(limit)
        self._pending: list[str] = []
        self._pending_len = 0

    @property
    def empty(self) -> bool:
        return not self._pending

    def take(self) -> str | None:
        """Return the pending batch as one wrapped message and clear it."""
        if not self._pending:
            return None
        content = _wrap("\n".join(self._pending))
        self._pending = []
        self._pending_len = 0
        if len(content) > self.limit:  # defensive invariant
            raise AssertionError("batch produced an oversized Discord payload")
        return content

    def add(self, message: str) -> list[str]:
        """Add one journal entry; return wrapped messages that must be sent now, in order.

        A non-empty result always starts with the previously pending batch (if
        any), followed by any non-final segments of this entry. This entry's
        final segment is always left pending.
        """
        payloads = _entry_payloads(message, budget=self._budget)
        if len(payloads) == 1 and self._pending:
            candidate = self._pending_len + 1 + len(payloads[0])
            if candidate <= self._budget:
                self._pending.append(payloads[0])
                self._pending_len = candidate
                return []
        ready: list[str] = []
        flushed = self.take()
        if flushed is not None:
            ready.append(flushed)
        ready.extend(_wrap(payload) for payload in payloads[:-1])
        self._pending = [payloads[-1]]
        self._pending_len = len(payloads[-1])
        return ready


def gap_marker(
    *,
    limit: int = DEFAULT_LIMIT,
    reason: str = "checkpoint was too old to resume safely",
) -> str:
    content = _wrap(
        f"[Botman] Live-log {reason}; continuing from the live tail. "
        "Use /logs download for the skipped interval."
    )
    if len(content) > limit:
        raise AssertionError("gap marker exceeds Discord limit")
    return content
