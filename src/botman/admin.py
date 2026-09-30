"""Admin identity parsing shared by future slash-only admin commands."""

from __future__ import annotations


def parse_admin_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    result: set[int] = set()
    for item in raw.replace("\n", ",").split(","):
        value = item.strip()
        if not value:
            continue
        if not value.isdigit():
            raise ValueError(f"ADMIN_IDS contains a non-numeric value: {value!r}")
        result.add(int(value))
    return frozenset(result)
