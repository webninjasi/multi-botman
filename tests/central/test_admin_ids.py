from botman.admin import parse_admin_ids


def test_admin_ids_whitespace_is_stripped() -> None:
    assert parse_admin_ids(" 123, 456 ,\n789 ,, ") == frozenset({123, 456, 789})
