# 2026-09-30 07:17 — Static quality gate wiring

- Added pinned Ruff and mypy development dependencies and repository configuration.
- CI now has a dedicated Python 3.11 quality job for `ruff check src tests scripts` and `mypy src`, separate from the Python 3.11-3.13 unit/build matrix.
- Removed obvious unused imports surfaced by a local AST sanity pass ahead of Ruff.
- The execution environment at the time could not download Ruff/mypy, so this revision did not claim that their first real run passed; CI or a network-enabled development environment must execute that gate.
