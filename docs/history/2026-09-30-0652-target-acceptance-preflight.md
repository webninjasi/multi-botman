# 2026-09-30 06:52 — Target acceptance preflight

- Added `botman-target-preflight` to make the real-VPS readiness gate repeatable before Discord acceptance.
- The preflight checks the configured Compose command, management/agent journal groups, protected agent directory modes, persistent journald policy/cap, cysystemd 2.x availability, direct system-journal access, installed/enabled agent unit, and the exact non-interactive sudo status command used by central.
- Optional `--require-active --app STACK.APP` checks the running agent plus an actual `botman-log-export` read through the protected stack-qualified app mapping.
- Added wheel-entry verification and focused tests for the new CLI. Automated suite is now 162 tests; real-host acceptance remains outstanding.
