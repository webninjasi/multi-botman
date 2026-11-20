# HANDOFF — Botman Fresh Start

> Keep this file concise/current. Detailed rationale belongs in the linked docs; dated change notes belong in [`history/`](history/).

## Current status

- No production deployment exists; backward compatibility is not required.
- Archived code under `reference/original-ag-botman/` is reference-only.
- Fresh implementation is active.
- Current automated suite: **176 passed** (before live-log batching tests were added; re-count on Linux).
- Phase 1 is complete.
- Phase 2 central Compose/lifecycle/Discord core is complete at unit level.
- Phase 3 central Git + `/update` deployment core and Discord adapter are complete at unit level.
- Phase 4 live log-agent core/runtime is implemented and unit-tested with fake journal/HTTP adapters. Live delivery batches entries per Discord message (size or `live_batch_max_delay_sec`).
- Phase 5 central live-log control is implemented; repeatable Docker/Podman-aware target provisioning now exists in `scripts/setup.sh`, with manual alternatives documented.
- Phase 6 historical exporter plus `/logs tail|download` is implemented and unit-tested.
- Phase 7 env/config onboarding, including conservative server/stack/app edit UX, is implemented.
- Phase 8 packaging/docs are in progress; central/agent units, clean-checkout CI, wheel-content verification, target-host preflight, and Ruff/mypy CI gates exist. Ruff and mypy pass locally as of 2026-10-26. Real-host acceptance remains.

Real Docker, Podman, cysystemd/journald, and Discord test-guild acceptance have not yet been run in this build environment. `.github/workflows/runtime-integration.yml` runs Compose deployment/lifecycle acceptance (`tests/integration/`) green in CI on Docker Compose and on Podman with both the docker-compose and podman-compose providers.

The test suite is Linux-only: on Windows, POSIX path, timezone, `fchmod`, and `grp` tests fail. Use CI as the source of truth.

## Documentation map

All docs live in `docs/` (this folder); `README.md` stays at the repo root.

| Doc | Use it for |
| --- | --- |
| [`DECISIONS.md`](DECISIONS.md) | Authoritative approved product/architecture choices |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System components and data flows |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | Phased implementation sequence |
| [`CONFIG_AND_COMMANDS.md`](CONFIG_AND_COMMANDS.md) | Central config model and Discord command UX |
| [`LOGGING.md`](LOGGING.md) | Live log agent, batching, checkpoints, historical export |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Git SHA -> archive -> staged release -> Compose deployment |
| [`SECURITY.md`](SECURITY.md) | Trust boundaries and security requirements |
| [`TEST_PLAN.md`](TEST_PLAN.md) | Acceptance, integration, and failure drills |
| [`VPS_AND_DISCORD_SETUP.md`](VPS_AND_DISCORD_SETUP.md) | Operator bootstrap/onboarding runbook |
| [`MANIFEST.md`](MANIFEST.md) | What each source/packaging file is |
| [`ROADMAP.md`](ROADMAP.md) | Deferred work |
| [`REFERENCES.md`](REFERENCES.md) | External library/platform references |
| [`history/`](history/) | Dated per-feature change notes and past pivots (`YYYY-MM-DD-HHMM-<topic>.md`, sorts chronologically) |

## Product goal

One central Discord bot manages Docker/Podman Compose applications across small local/remote VPSes using transient SSH/SFTP. One tiny per-VPS daemon handles **live journald -> Discord log delivery only**. Historical reads use a short-lived helper from the same target package. No management web ports, persistent SSH log streams, database, Redis, Loki/Grafana, or comparable control-plane infrastructure.

## Non-negotiable decisions

Read [`DECISIONS.md`](DECISIONS.md) for full detail. Key constraints:

- Remote management is transient SSH/SFTP with strict host verification.
- Stack command channel is the lifecycle/log/update authorization boundary; commands resolve channel -> stack before looking up an app.
- App names are stack-local, not global. Different stacks may both use names such as `app`, `db`, or `worker`.
- Stack and app are distinct: one stack has one server/channel/Compose project; multiple independently versioned apps may share it.
- Compose is Botman-managed, not app-repo-owned. The installed file is the source of truth: an app missing from it is disabled (commands refused, config/source kept until explicit deletion).
- Git credentials stay on central; each app gets a separate central deploy key.
- `/update` is v1's only deployment trigger.
- Deployment uses exact Git SHA -> archive -> checksum -> staged release -> atomic `current` -> service-only build/up with rollback attempt.
- Live log reading uses cysystemd 2.x `AsyncJournalReader`, not `journalctl` or container CLI streams.
- Live logging has no unbounded producer queue; delivery backpressure stops that app from reading ahead.
- Cursor advances only after all Discord segments representing the journal entry are acknowledged.
- New/long-gap live sessions tail instead of flooding missed history; historical `/logs download` is the recovery path.
- Env/config changes do not implicitly restart/deploy apps (`/apply APP` or, for build-time values, `/rebuild APP` applies them explicitly); runtime-affecting stack/env/Git mutations serialize with stack operations, and queued lifecycle/deploy/Compose work reloads config after obtaining the lock.
- Every slash/hybrid invocation must get a reply; `commands/errors.py` covers unhandled exceptions with a generic message (exception type only, never its text).
- Admin/secret workflows are slash-only and ephemeral; lifecycle/update/log UX remains hybrid where useful.

## Production layout

`/opt/botman` is the Git checkout, `/opt/botman-venv` the central venv, and `/opt/botman-agent-venv` the target agent/exporter venv. `scripts/setup.sh` provisions central/target hosts (auto-detecting Docker or Podman; `--runtime docker|podman` when both exist) and `scripts/update.sh` performs fast-forward updates. A target registered through Discord must use the matching `compose_argv`. Central and agents must be updated together because the agent config rejects unknown keys.

## Implemented central commands

Hybrid slash/prefix:

- `/start APP` / `!start APP`
- `/stop APP` / `!stop APP`
- `/restart APP` / `!restart APP`
- `/status APP` / `!status APP`
- `/apply APP` / `!apply APP` (service-scoped `up -d`; applies env/Compose edits)
- `/rebuild APP [build_only]` / `!rebuild APP [build_only]` (service-scoped `build` then `up -d` on the active release, i.e. `up -d --build`; `build_only` skips the recreate; for build-time env/args)

All Botman builds pass the app env file as `--env-file` when it exists, so `build.args` `${VAR}` can read `/env` values; `up` does not (see `CONFIG_AND_COMMANDS.md`). The `up` after a build (`/update`, its rollback, `/rebuild`) uses `--force-recreate`, because podman-compose otherwise keeps the old container. `/status` falls back to whole-stack `ps` when the provider rejects a service argument.
- `/update APP` / `!update APP`
- `/livelogs start|stop APP` / prefix equivalents
- `/logs tail APP [lines]` / prefix equivalent
- `/logs download APP from_time to_time [human|jsonl]` / prefix equivalent

Slash/admin/ephemeral (stack/app-scoped commands infer the stack from the current channel):

- `/config server add|edit|test`
- `/config stack add|edit`
- `/config app add|edit|remove` (`remove` only for disabled apps; preview + confirm; never volumes)
- `/config compose upload|show|apply` (`apply` previews, then removes dropped services' containers and runs `up -d` for the rest; never deletes volumes/networks)
- `/config git setup APP` / `/config git rotate-key APP`
- `/config agent sync|status`

Slash/ephemeral, stack-channel authorized like `/update` (not `ADMIN_IDS`-gated):

- `/env show|upload|set|unset`

See [`VPS_AND_DISCORD_SETUP.md`](VPS_AND_DISCORD_SETUP.md) for the exact onboarding order.

## Immediate next work

1. Confirm CI is green on Linux (tests on 3.11-3.13, Ruff, mypy) and record the new test count here.
2. Run `botman-target-preflight` on each real target, then run actual Linux/cysystemd integration for the live reader and historical exporter, including retained-history and journal-permission checks.
3. Repeat Docker and Podman Compose acceptance on real targets over SSH (CI covers the local-executor path only).
4. Run a Discord test-guild acceptance pass for hybrid command registration, threads, webhooks, upload limits, archived/locked thread behavior, the `/config compose apply` and upload **Apply now** buttons, and live-log batching cadence against webhook rate limits.
5. Exercise `scripts/setup.sh` and `scripts/update.sh` on the real central and second target VPS, including reboot persistence of the rootless Podman socket.
6. Reconcile runtime-specific findings before v1 completion.
7. Optionally add an opt-in stack `prune` for volumes/networks no longer referenced by the Compose file.

Do **not** repair or reuse archived `py/agent/log_agent.py` or deployment code as the implementation baseline.

## Handoff maintenance rules

- Update this file after each completed phase/material decision; keep it to current state only.
- For each feature or meaningful chunk of change, add a note to `history/` named `YYYY-MM-DD-HHMM-<topic>.md` (date/time of the change). Do not append dated sections here.
- Keep [`DECISIONS.md`](DECISIONS.md) authoritative for approved choices.
- Put detailed rationale in subsystem docs.
- Record deferred work in [`ROADMAP.md`](ROADMAP.md).
- Do not call v1 complete until [`TEST_PLAN.md`](TEST_PLAN.md) real integration/failure drills pass.
