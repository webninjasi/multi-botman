## 2026-09-30 — Env authorization aligned with deployers

- Moved `/env show|upload|set|unset` out of the administrator command registration path.
- `/env` now uses the same stack-channel/app authorization boundary as `/update` instead of requiring `ADMIN_IDS`; commands remain slash-only and ephemeral because they may expose secrets.
- Added Discord registration/authorization regression coverage; automated suite is now 170 tests.


## 2026-09-30 — Git-layout installer/updater and configurable log exporter

- Standardized production venvs on `/opt/botman-venv` and `/opt/botman-agent-venv`; shipped systemd units now use those paths.
- Added `scripts/setup.sh` for repeatable central/target/all installation, including rootless `botmgr` Podman user-manager/socket setup, journald, agent permissions, venvs, units, and restricted sudoers.
- Added `scripts/update.sh` for clean fast-forward Git updates, venv reinstalls, unit refresh, daemon reload, and configured-service restarts.
- Added `settings.log_export_bin` and `settings.agent_config_path`; historical logs and agent sync now use configured paths rather than independent hardcoded constants.
# Project History and Pivots

## 2026-09-30 — Static quality gate wiring

- Added pinned Ruff and mypy development dependencies and repository configuration.
- CI now has a dedicated Python 3.11 quality job for `ruff check src tests scripts` and `mypy src`, separate from the Python 3.11-3.13 unit/build matrix.
- Removed obvious unused imports surfaced by a local AST sanity pass ahead of Ruff.
- The current execution environment cannot download Ruff/mypy, so this revision does not claim that their first real run passed; CI or a network-enabled development environment must execute that gate next.

## 2026-09-30 — Target acceptance preflight

- Added `botman-target-preflight` to make the real-VPS readiness gate repeatable before Discord acceptance.
- The preflight checks the configured Compose command, management/agent journal groups, protected agent directory modes, persistent journald policy/cap, cysystemd 2.x availability, direct system-journal access, installed/enabled agent unit, and the exact non-interactive sudo status command used by central.
- Optional `--require-active --app STACK.APP` checks the running agent plus an actual `botman-log-export` read through the protected stack-qualified app mapping.
- Added wheel-entry verification and focused tests for the new CLI. Automated suite is now 162 tests; real-host acceptance remains outstanding.

## 2026-09-30 — Release and concurrency hardening

- Made tests runnable from a clean source checkout without relying on an editable install, added Python 3.11-3.13 CI for tests/compile/wheel build, and added a wheel-content/entry-point verifier.
- Hardened deployment archive validation against escaping symlinks, hard links, and malformed active `current` targets.
- Made Git deploy-key rotation stage and validate the replacement before swapping out the existing keypair.
- Enforced Compose project-name uniqueness per server and managed-service uniqueness within each stack.
- Hardened live-log recovery for unusable cursors, corrupt checkpoints, and unexpectedly ended reader streams.
- Serialized server/stack/app config edits, Git-key setup, `.env` mutations, and manual agent sync against the corresponding runtime operation locks.
- `.env set`/`unset` now hold the stack lock across the complete read-modify-write transaction.
- Store-backed lifecycle, deployment, and Compose upload paths reload/re-authorize config after acquiring the stack lock, preventing queued commands from acting on a stale service/branch snapshot after an admin edit.
- Automated suite is now 155 tests. Real container/systemd/journald/Discord acceptance remains outstanding.

## 2026-09-30 — Config edit UX

- Added `/config server edit`, `/config stack edit`, and `/config app edit`.
- Stack/app edits resolve stack identity from the invoking channel; no global app lookup or stack option was reintroduced.
- Server edits preserve unspecified fields and support explicit known-hosts-path clearing without disabling SSH host verification.
- Established stack identity/location edits are rejected once apps exist to avoid silent partial migrations.
- App journal identifiers cannot change while live logging is active, preventing target-agent subscription drift.
- Added focused regression coverage; automated suite is now 127 tests.
- Added deterministic formatter fuzz coverage for Discord payload budget/order preservation.


## Legacy JavaScript prototype

The original Node.js implementation streamed `docker/podman compose logs -f` through long-lived SSH processes.

Observed problems included:

- fragile SSH log streams
- reconnect loops
- duplicate `--tail` replay
- Discord rate-limit cascades
- remote process/resource leakage

This implementation remains under `reference/original-ag-botman/js/` only.

## First Python prototype

The first Python rewrite established several good product ideas:

- central Discord bot
- transient SSH for management
- one local log agent per VPS
- journald as the durable local source
- Discord webhook live-log threads
- admin config/env commands
- Git-based `/update`

However it accumulated complexity and correctness/security issues:

- `journalctl` child supervision and JSON parsing
- queue + unbounded secondary buffer during sink outage
- complex giant-line fragmentation/checkpoint accounting
- lifecycle channel check accidentally relaxed
- insecure SSH host verification in the executor
- Git credentials/host-key setup on every target VPS
- `.env` destructive edge cases
- incomplete fresh-server agent dependency provisioning
- documentation/tests overstating actual behavior

See `reference/REVIEW_FINDINGS.md`.

## Fresh-start decision (2026-09-30)

Because nothing had been deployed, the project deliberately chose not to preserve compatibility.

### Logging pivot

- replace spawned `journalctl` with `cysystemd` 2.x direct journal access
- no producer queue/unbounded buffer
- stable `SYSLOG_IDENTIFIER` from journald tag
- explicit persistent `/livelogs start|stop`
- split multi-line live entries at line boundaries; truncate only an individually oversized line
- add historical `/logs download` from journald in human/JSONL formats
- keep local persistent journald capped at 1 GiB until external archival exists

### Deployment pivot

- logging agent is logging-only
- Git access/credentials stay on central VPS
- central fetches exact app commit and ships source archive via SFTP
- target release staging/current symlink model
- introduce stack model because multiple independently versioned apps share one Compose file/channel
- Compose remains Botman-managed rather than repo-owned
- `/update` only for v1; push webhook/HMAC deferred

### UX decisions

- keep useful prefix lifecycle commands through discord.py hybrid commands
- admin/secret workflows slash-only/ephemeral
- remove raw `.env` modal editor
- deployment output gets a thread under the command channel + full transcript attachment
- command channel remains the authorization boundary

## Current state

The fresh implementation includes central lifecycle/Compose control, Discord adapters, Git-SHA deployments with rollback, env/config onboarding and edits, live journald delivery, and historical exports. Automated unit coverage is complete for these paths; real Docker/Podman, journald/cysystemd, and Discord test-guild acceptance remains before v1 is declared complete. The archived code remains reference-only.

## Fresh implementation progress (2026-09-30)

The new codebase moved through the first deployment-capable slice:

- strict central config/persistence/routing/executor foundation
- stack-aware Compose management with runtime validation and operation locks
- real discord.py lifecycle/admin/update registration
- central Git-over-SSH deploy-key/cache path
- exact-SHA archive deployment with checksum, staged releases, atomic current switch, service-only build/up, rollback attempts, and transcripts
- slash-only env/config onboarding commands
- new cysystemd-based log-agent core with no producer queue, line-aware Discord formatting, fatal-destination suspension, and atomic cursor checkpoints
- systemd units and a corrected VPS/Discord setup runbook

The log-agent is intentionally a fresh implementation; no `journalctl` subprocess or archived queue/buffer design was carried forward.


## Fresh implementation continuation (2026-09-30)

- Added persistent central `/livelogs start|stop` control with webhook/thread repair, server-scoped synchronization, and rollback if the target agent cannot be restarted.
- Added `/config agent sync|status`; target OS/package provisioning remains an explicit one-time manual runbook step.
- Added `botman-log-export` using cysystemd finite journal reads, protected app-to-identifier lookup, complete human/JSONL gzip exports, size-bounded parts, and empty-range artifacts.
- Added hybrid `/logs tail|download` with app-channel authorization, IANA-timezone conversion, DST gap/ambiguity rejection, target-temp cleanup, and Discord upload budgeting.
- Hardened agent service control to non-interactive `sudo -n` and serialized live-log mutation+sync per target server.
- Automated suite reached 113 passing tests; real VPS/container/journald/Discord acceptance remains outstanding.


## Stack-local app namespace correction (2026-09-30)

- Moved managed apps under each stack in the central YAML model; app names are no longer globally unique.
- Runtime commands now resolve Discord channel -> stack first, then resolve the app only within that stack.
- `/config app add`, Compose upload/show, Git setup/rotation, and `/env` infer the stack from the command channel instead of asking for a stack argument.
- Namespaced central Git caches and deploy keys by stack (`repos/<stack>/<app>.git`, `keys/<stack>/<app>`).
- Namespaced target log-agent/export identities as `<stack>.<app>` so duplicate app names on one VPS do not share checkpoints or exporter entries.
- Added one-way loading support for the earlier flat top-level `apps:` schema; the next config save writes the corrected nested representation.
- Added regression coverage for duplicate `app`/`db` names across stacks, Discord command registration, Git/key paths, and target agent config.
- Automated suite reached 118 passing tests.


## Git deploy-key argv validation fix (2026-09-30)

- Fixed `LocalGitRunner` so legitimate empty subprocess arguments are allowed, including the required `ssh-keygen -N ""` used by `/config git setup`.
- Empty executable names and NUL-containing arguments remain rejected.
- Added regression coverage for both behaviors.
- Added `reference/` to `.gitignore`.

## Env slash-command annotation fix (2026-09-30)

- Fixed `/env` command registration under discord.py when postponed annotations caused `discord.Interaction` / `discord.Attachment` to be resolved from module globals where `discord` was not defined.
- `/env` now exposes concrete runtime Discord annotation types when commands are registered.
- Added regression coverage for slash-command annotation resolution.

