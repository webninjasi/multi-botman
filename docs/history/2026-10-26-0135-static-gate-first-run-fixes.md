# 2026-10-26 01:35 — First Ruff/mypy run fixes and `LOCAL_ONLY` test doubles

First real run of the CI quality gates (`ruff check src tests scripts`, `mypy src`).

## Bug fixed

- `/config compose show` used `io.BytesIO` without importing `io` (`src/botman/commands/admin.py`). It raised `NameError` whenever it sent the Compose file as an attachment. Ruff F821 now guards this.

## Type-check configuration

- mypy now checks with `platform = "linux"`, because central and agent use Linux-only APIs (`pwd`, `grp`, `os.fchmod`).
- Added `types-PyYAML` to the `dev` extra.
- Removed `# type: ignore[import-not-found]` comments that `ignore_missing_imports = true` already made redundant (they tripped `warn_unused_ignores`), plus one unused `misc` ignore in `deployment.py`.
- Narrowed `proc.returncode` after `communicate()` in `executor.py` and `git.py`; annotated the Compose build `context` as `object` before its `isinstance` check.

## Tests

- The journal/export test doubles now expose `LOCAL_ONLY`, matching the 2026-10-03 reader change.
- Several tests still fail on Windows only: POSIX absolute paths, missing tz database, `os.fchmod`, `grp`. CI on Linux is authoritative.
