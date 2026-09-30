# Full Implementation Plan

## Strategy

Build a fresh implementation next to `reference/`. Reuse ideas and small proven helpers only after re-review; do not evolve the old module graph in place.

Suggested repository layout:

```text
gpt-botman/                  # planning package now; future source repo may use root directly
├── src/
│   ├── botman/
│   │   ├── __init__.py
│   │   ├── bot.py
│   │   ├── config.py
│   │   ├── models.py
│   │   ├── executor.py
│   │   ├── compose.py
│   │   ├── deployment.py
│   │   ├── provisioning.py
│   │   ├── discord_output.py
│   │   ├── webhooks.py
│   │   └── commands/
│   │       ├── lifecycle.py
│   │       ├── admin_config.py
│   │       └── admin_env.py
│   └── botman_agent/
│       ├── __init__.py
│       ├── config.py
│       ├── journal.py
│       ├── formatting.py
│       ├── discord_sink.py
│       ├── live.py
│       ├── state.py
│       └── export.py
├── tests/
│   ├── central/
│   ├── agent/
│   └── integration/
├── systemd/
│   ├── botman.service
│   └── botman-log-agent.service
└── pyproject.toml
```

Names can change; separation of responsibilities should not.

## Phase 1 — Models, config, secure executor — COMPLETE

### Implement

- `SettingsConfig`
- `ServerConfig`
- `StackConfig`
- `AppConfig` (+ Git/log/live nested models)
- strict validation and unknown-key rejection
- config atomic save, `0600`, mutation lock
- query helpers: app->stack->server; apps for command channel/server
- `ExecResult`
- `LocalExecutor`
- `SSHExecutor`
- argv-based `run()` and line-streaming `stream()`
- SFTP read/write/upload/download
- strict SSH host verification and connection timeout
- safe privileged write helper with checked exit codes

### Tests / acceptance

- malformed config fails clearly
- path/name/port/timezone validations
- concurrent config mutation test
- local argv preserves spaces/metacharacters without shell interpretation
- SSH mock verifies `known_hosts` is not disabled
- SSH connect timeout included
- privileged write surfaces move/chmod failure
- channel routing helper rejects wrong channel

Do not proceed until this phase passes.

## Phase 2 — Stack-aware Compose and command authorization — UNIT COMPLETE

### Implement

- standard stack filesystem path helpers
- compose argv = configured server prefix + explicit `-p <project>` + `-f <compose-file>`
- service-scoped `start`, `stop`, `restart`, `status`, `build`, `up`
- per-stack operation locks
- command-channel authorization decorator/helper used by **every** lifecycle/log/update entry point
- hybrid lifecycle commands for start/stop/restart/status
- concise output formatter + full transcript helper

### Compose upload

- slash/admin/ephemeral
- safe attachment size cap
- safe YAML parse
- write shared stack `compose.yml`
- validate managed services + logging tag + standard build context
- runtime `compose config` validation where feasible
- no automatic application restart/deployment

### Tests

- multiple apps share same stack/compose/channel
- wrong channel is rejected for slash and prefix paths
- operation lock serializes conflicting commands
- command argv exactness for Docker/Podman configured forms
- malicious service/stack names rejected or safely represented

## Phase 3 — Central Git cache and `/update` — UNIT COMPLETE

### Implement

- per-app central deploy-key creation/rotation
- public key returned ephemerally
- strict Git-provider host verification outside application source
- central bare repo/mirror initialization + fetch
- resolve configured branch to exact SHA
- remote current-SHA inspection
- no-op if unchanged
- `git archive` + checksum
- SFTP archive upload
- remote staging/extraction/checksum
- release metadata
- atomic `current` symlink management
- service-only build and `up -d --no-deps`
- rollback attempt on activation failure
- keep N recent releases
- unique temp paths + cleanup
- deployment thread in command channel
- complete transcript attachment/splitting
- per-stack deployment/lifecycle serialization

### Tests

Use temp local fake targets first, then real Compose integration:

- initial deploy
- same SHA no-op
- new SHA deploy
- archive checksum mismatch
- extraction failure leaves current untouched
- build failure restores old current and old container remains running
- up failure triggers/report rollback
- unrelated service is not restarted
- concurrent update/start serialized
- transcript produced on success/failure

## Phase 4 — Fresh log-agent package with cysystemd — CORE/UNIT COMPLETE

### Implement agent config

Per server, include every app's:

- app name
- `SYSLOG_IDENTIFIER`
- live enabled state
- webhook URL only when needed
- thread ID/subscription ID when enabled
- global live resume limit

### Implement journal wrapper

- `AsyncJournalReader`
- system journal open
- `Rule(SYSLOG_IDENTIFIER=...)`
- cursor/timestamp conversion
- tail/new-subscription behavior
- short-gap cursor resume
- no `journalctl`, Docker CLI, Podman CLI, or unbounded queue

### Implement Discord live sink

- shared aiohttp session
- per-webhook serialization/cooldown
- exact content-budget formatter
- whole-line splitting / huge-line truncation per `LOGGING.md`
- disable mentions
- transient retry blocks only that app task
- fatal destination status suspends the app task
- atomic state save after safe cursor advancement

### Agent supervisor

- one task per enabled app
- app task exception does not kill all streams
- bounded restart/backoff for unexpected reader/task failures
- systemd handles whole-process crash (`Restart=always`)
- graceful shutdown with short timeout; unsaved entries remain in journald

### Tests

See agent section in `TEST_PLAN.md`, especially:

- multi-line split ordering
- huge single line truncation
- checkpoint only after final segment of entry
- outage memory remains bounded
- fatal thread error suspends without hot loop
- short restart resume vs long-gap tail

## Phase 5 — `/livelogs` control and agent provisioning — CONTROL COMPLETE / PROVISIONING MANUAL

### Provisioning

- build/install agent package into dedicated venv
- install/verify `cysystemd` 2.x, aiohttp, PyYAML as required
- dedicated agent service account with journal read permission
- write agent config securely
- install systemd unit with restart policy
- verify service active after provisioning/restart
- configure/verify persistent journald + 1 GiB cap using an explicit drop-in

Provisioning must fail if a critical command fails; no “warning but success” behavior.

### `/livelogs start`

- channel authorization
- create/check/repair thread
- update persistent live state
- generate server agent config
- restart agent via SSH/local executor
- report thread

### `/livelogs stop`

- channel authorization
- disable state
- archive thread best-effort
- regenerate config/restart agent

### Tests

- start disabled app
- start already healthy app is idempotent
- start repairs archived thread when possible
- start replaces deleted/locked/unusable thread
- stop disables
- agent config contains correct server-local apps only

## Phase 6 — Historical `/logs` and exporter CLI — UNIT COMPLETE

### Implement `botman-log-export`

- same cysystemd journal wrapper
- app name resolved through protected agent config
- realtime seek from UTC timestamps
- end-time stop
- human and JSONL gzip output
- full message data; no Discord truncation
- part splitting
- metadata/reporting for empty ranges

### Central commands

- configurable timezone, default `Europe/Istanbul`
- parse local date/time -> aware datetime -> UTC
- `/logs tail` / prefix fallback retrieves recent N entries via helper
- `/logs download` executes exporter remotely, SFTP downloads all parts, sends attachments, and removes remote temp directory
- output remains app-channel-authorized

### Tests

- DST/zone parsing generally via zoneinfo (Istanbul currently fixed UTC+3 but code must not hard-code +03)
- start/end boundary ordering
- multiline preservation
- huge message preservation in download
- human/jsonl contents
- attachment part splitting
- cleanup after success and failure

## Phase 7 — Admin env/config UX — PARTIALLY PULLED FORWARD

### Env

- show/upload/set/unset only
- missing vs read failure distinction
- line-preserving edits
- key validation
- `0600`
- no implicit deploy/restart

### Config

Implement server/stack/app/Git/agent admin commands per `CONFIG_AND_COMMANDS.md`.

Important workflow tests:

1. add server
2. add stack in current command channel
3. add apps sharing stack
4. upload Compose
5. configure repo/deploy key
6. upload/set env
7. provision log agent
8. `/update`
9. `/livelogs start`
10. `/logs tail` + `/logs download`

## Phase 8 — Systemd, packaging, docs, manual acceptance — IN PROGRESS

- central dedicated-user unit + protected environment file
- agent dedicated-user unit + venv executable
- startup configuration validation
- lint/type checks
- complete automated test suite
- real Docker host test
- real Podman host test
- failure drills from `TEST_PLAN.md`
- update README/HANDOFF/HISTORY with actual implementation status

## Definition of v1 complete

All of the following are true:

- lifecycle routing cannot operate an app from the wrong command channel
- central SSH and Git SSH verify remote host identity
- multiple apps sharing a Compose stack deploy independently
- target VPSes contain no Git provider credential
- deployment has tested staging/no-op/build-failure/rollback paths
- log agent contains no spawned `journalctl`/Docker/Podman reader process
- prolonged Discord outage does not cause unbounded Python memory
- line-aware live formatting preserves order and only truncates genuinely oversized single lines
- explicit live start/stop persists across process restarts
- historical human/jsonl downloads work from journald and split attachments
- journald persistence/cap is verified
- env operations cannot overwrite a valid file after an unrelated read failure
- all automated tests pass and real Docker/Podman checks pass
