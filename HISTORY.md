# Project History and Pivots

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

Planning is complete and fresh implementation has started. Phase 1 (models/config/security executor/routing) is complete. Phase 2 Compose command construction, stack locking, lifecycle core, and Compose validation are implemented; Discord command adapters and upload transport remain. The archived code remains reference-only.

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
