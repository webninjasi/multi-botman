# 2026-09-30 12:51 — Setup installs Python 3.11+ when missing

- `scripts/setup.sh` now installs a supported interpreter when no Python 3.11+ is found, instead of failing outright.
- apt hosts try `python3.13`, `python3.12`, then `python3.11` (with `-venv`/`-dev`). On Ubuntu releases whose repositories lack them, setup enables the deadsnakes PPA and installs `python3.11`.
- dnf hosts try the same versions (with `-devel` when available).
- If installation still fails, setup stops with a message to install Python 3.11+ manually or set `PYTHON_BIN`.
- Added installation-logic coverage in `tests/test_setup_script.py`.
