# Package Manifest

Updated 2026-09-30 for the fresh Botman implementation.

## Central implementation

- `src/botman/models.py` — strict central config model/derived paths
- `src/botman/config.py` — atomic config persistence/mutation lock
- `src/botman/routing.py` — channel -> stack -> stack-local app/server resolution and authorization
- `src/botman/executor.py` — local + strict SSH/SFTP execution
- `src/botman/compose.py` — stack-scoped Compose/lifecycle/upload validation
- `src/botman/git.py` — stack-namespaced per-app deploy keys and strict central Git cache
- `src/botman/deployment.py` — release archive/staging/activation/rollback/pruning
- `src/botman/env.py` — protected env file show/upload/set/unset
- `src/botman/admin_config.py` — server/stack/app admin config services
- `src/botman/agent_control.py` — per-server log-agent config generation/restart/status
- `src/botman/live_logs.py` — persistent live-log state and synchronized target updates
- `src/botman/logs.py` — historical tail/download service and local-time conversion
- `src/botman/discord_output.py` — concise output and complete attachment transcripts
- `src/botman/bot.py` — discord.py entrypoint/runtime wiring
- `src/botman/commands/lifecycle.py`
- `src/botman/commands/update.py`
- `src/botman/commands/live_logs.py`
- `src/botman/commands/logs.py`
- `src/botman/commands/admin.py`

## Target log-agent/export implementation

- `src/botman_agent/config.py` — strict protected agent config
- `src/botman_agent/state.py` — atomic per-stack/app cursor state/restart decision
- `src/botman_agent/journal.py` — cysystemd 2.x direct async live wrapper
- `src/botman_agent/formatting.py` — Discord content budgeting/line-aware split
- `src/botman_agent/discord_sink.py` — backpressured webhook delivery/retry/fatal classification
- `src/botman_agent/live.py` — per-app delivery/checkpoint/supervision
- `src/botman_agent/main.py` — executable multi-app live agent runtime
- `src/botman_agent/export.py` — finite cysystemd historical reader, gzip part exporter, CLI

## Packaging/runtime

- `pyproject.toml`
- `systemd/botman.service`
- `systemd/botman-log-agent.service`
- `config.example.yaml`

## Tests

- `tests/central/`
- `tests/agent/`

Current fresh result: **118 passed**.

## Operator/design documentation

- `VPS_AND_DISCORD_SETUP.md`
- `README.md`
- `HANDOFF.md`
- `DECISIONS.md`
- `ARCHITECTURE.md`
- `IMPLEMENTATION_PLAN.md`
- `LOGGING.md`
- `DEPLOYMENT.md`
- `CONFIG_AND_COMMANDS.md`
- `SECURITY.md`
- `TEST_PLAN.md`
- `ROADMAP.md`
- `REFERENCES.md`
- `HISTORY.md`

## Archived reference

- `reference/REVIEW_FINDINGS.md`
- `reference/original-ag-botman/`
- `reference/original-ag-botman-1.zip`

Original archived upload SHA-256:

`1a794f09455b87d598fa6d8e2ef024d68444674a9d76d2575998a94588b20c8d`
