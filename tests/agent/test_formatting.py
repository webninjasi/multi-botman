from botman_agent.formatting import render_entry


def unwrap(content: str) -> str:
    assert content.startswith("```text\n") and content.endswith("\n```")
    return content[len("```text\n") : -len("\n```")]


def test_multiline_splits_only_between_lines_and_preserves_order():
    message = "alpha\n" + ("b" * 30) + "\nomega"
    parts = render_entry(message, limit=36)
    assert [unwrap(p.content) for p in parts] == ["alpha", "b" * 5 + " … [line truncated]", "omega"]
    assert [p.final_for_entry for p in parts] == [False, False, True]
    assert all(len(p.content) <= 36 for p in parts)


def test_huge_line_truncates_but_following_line_survives():
    parts = render_entry("x" * 3000 + "\nafter")
    assert len(parts) == 2
    assert "line truncated for Discord" in parts[0].content
    assert "original length=3000" in parts[0].content
    assert unwrap(parts[1].content) == "after"
    assert all(len(p.content) <= 2000 for p in parts)


def test_embedded_fence_cannot_break_wrapper_or_budget():
    parts = render_entry("hello ``` @everyone\nworld")
    assert len(parts) == 1
    assert "``` @everyone" not in unwrap(parts[0].content)
    assert "``\u200b` @everyone" in parts[0].content
    assert len(parts[0].content) <= 2000


def test_non_huge_lines_appear_once_under_small_budget():
    lines = ["one", "two-two", "three", "four-four"]
    parts = render_entry("\n".join(lines), limit=25)
    recovered = []
    for part in parts:
        recovered.extend(unwrap(part.content).split("\n"))
    assert recovered == lines


def test_deterministic_fuzz_preserves_non_huge_lines_and_budget():
    import random

    from botman_agent.formatting import CODE_CLOSE, CODE_OPEN, sanitize_markdown

    rng = random.Random(20260930)
    alphabet = "abcXYZ012 -_/@`"
    for _ in range(400):
        limit = rng.randint(40, 220)
        budget = limit - len(CODE_OPEN) - len(CODE_CLOSE)
        line_count = rng.randint(1, 20)
        raw_lines = []
        for _line in range(line_count):
            # Leave enough headroom for markdown fence sanitization to add a
            # character without turning this into a deliberate huge-line case.
            length = rng.randint(0, max(0, budget - 8))
            line = "".join(rng.choice(alphabet) for _ in range(length))
            raw_lines.append(line)

        parts = render_entry("\n".join(raw_lines), limit=limit)
        assert parts
        assert all(len(part.content) <= limit for part in parts)
        assert [part.final_for_entry for part in parts] == [False] * (len(parts) - 1) + [True]

        recovered = []
        for part in parts:
            recovered.extend(unwrap(part.content).split("\n"))
        assert recovered == [sanitize_markdown(line) for line in raw_lines]
