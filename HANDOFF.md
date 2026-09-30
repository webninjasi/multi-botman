# HANDOFF — Botman Fresh Start

> Keep this file concise/current. Detailed rationale belongs in the linked docs.

## Current status

- No production deployment exists; backward compatibility is not required.
- Archived code under `reference/original-ag-botman/` is reference-only.
- Fresh implementation is active.
- Current automated suite: **170 passed**.
- Phase 1 is complete.
- Phase 2 central Compose/lifecycle/Discord core is complete at unit level.
- Phase 3 central Git + `/update` deployment core and Discord adapter are complete at unit level.
- Phase 4 live log-agent core/runtime is implemented and unit-tested with fake journal/HTTP adapters.
- Phase 5 central live-log control is implemented; repeatable rootless-Podman target provisioning now exists in `scripts/setup.sh`, with manual alternatives documented.
- Phase 6 historical exporter plus `/logs tail|download` is implemented and unit-tested.
- Phase 7 env/config onboarding, including conservative server/stack/app edit UX, is implemented.
- Phase 8 packaging/docs are in progress; central/agent units, clean-checkout CI, wheel-content verification, target-host preflight, and Ruff/mypy CI gates exist. The quality tools could not be installed in this offline build, so their first actual run plus real-host acceptance remain.

Real Docker, Podman, cysystemd/journald, and Discord test-guild acceptance have not yet been run in this build environment.

## Product goal

One central Discord bot manages Docker/Podman Compose applications across small local/remote VPSes using transient SSH/SFTP. One tiny per-VPS daemon handles **live journald -> Discord log delivery only**. Historical reads use a short-lived helper from the same target package. No management web ports, persistent SSH log streams, database, Redis, Loki/Grafana, or comparable control-plane infrastructure.

## Non-negotiable decisions

Read `DECISIONS.md` for full detail. Key constraints:

- Remote management is transient SSH/SFTP with strict host verification.
- Stack command channel is the lifecycle/log/update authorization boundary; commands resolve channel -> stack before looking up an app.
- App names are stack-local, not global. Different stacks may both use names such as `app`, `db`, or `worker`.
- Stack and app are distinct: one stack has one server/channel/Compose project; multiple independently versioned apps may share it.
- Compose is Botman-managed, not app-repo-owned.
- Git credentials stay on central; each app gets a separate central deploy key.
- `/update` is v1's only deployment trigger.
- Deployment uses exact Git SHA -> archive -> checksum -> staged release -> atomic `current` -> service-only build/up with rollback attempt.
- Live log reading uses cysystemd 2.x `AsyncJournalReader`, not `journalctl` or container CLI streams.
- Live logging has no unbounded producer queue; delivery backpressure stops that app from reading ahead.
- Cursor advances only after all Discord segments representing the journal entry are acknowledged.
- New/long-gap live sessions tail instead of flooding missed history; historical `/logs download` is the recovery path.
- Env/config changes do not implicitly restart/deploy apps; runtime-affecting stack/env/Git mutations serialize with stack operations, and queued lifecycle/deploy/Compose work reloads config after obtaining the lock.
- Admin/secret workflows are slash-only and ephemeral; lifecycle/update/log UX remains hybrid where useful.

## Implemented central commands

Hybrid slash/prefix:

- `/start APP` / `!start APP`
- `/stop APP` / `!stop APP`
- `/restart APP` / `!restart APP`
- `/status APP` / `!status APP`
- `/update APP` / `!update APP`
- `/livelogs start|stop APP` / prefix equivalents
- `/logs tail APP [lines]` / prefix equivalent
- `/logs download APP from_time to_time [human|jsonl]` / prefix equivalent

Slash/admin/ephemeral (stack/app-scoped commands infer the stack from the current channel):

- `/config server add|edit|test`
- `/config stack add|edit`
- `/config app add|edit`
- `/config compose upload|show`
- `/config git setup APP` / `/config git rotate-key APP`
- `/config agent sync|status`

Slash/ephemeral, stack-channel authorized like `/update` (not `ADMIN_IDS`-gated):

- `/env show|upload|set|unset`

See `VPS_AND_DISCORD_SETUP.md` for the exact onboarding order.

## Immediate next work

1. Run the configured `python -m ruff check src tests scripts` and `python -m mypy src` gates in a network-enabled dev/CI environment and resolve any first-run findings.
2. Run `botman-target-preflight` on each real target, then run actual Linux/cysystemd integration for the live reader and historical exporter, including retained-history and journal-permission checks.
3. Run real Docker and Podman Compose deployment/lifecycle/failure acceptance.
4. Run a Discord test-guild acceptance pass for hybrid command registration, threads, webhooks, upload limits, and archived/locked thread behavior.
5. Exercise `scripts/setup.sh` and `scripts/update.sh` on the real central and second target VPS, including reboot persistence of the rootless Podman socket.
6. Reconcile runtime-specific findings before v1 completion.

Do **not** repair or reuse archived `py/agent/log_agent.py` or deployment code as the implementation baseline.

## Handoff maintenance rules

- Update this file after each completed phase/material decision.
- Keep `DECISIONS.md` authoritative for approved choices.
- Put detailed rationale in subsystem docs.
- Record deferred work in `ROADMAP.md`.
- Do not call v1 complete until `TEST_PLAN.md` real integration/failure drills pass.


## 2026-09-30 deploy-key setup fix

`/config git setup` previously failed before launching `ssh-keygen` because `LocalGitRunner` rejected the intentional empty passphrase argument in `ssh-keygen -N ""`. The validator now allows empty non-executable arguments while still rejecting an empty executable and NUL bytes. `reference/` is now ignored by Git.


## 2026-09-30 canonical Git install/update layout

Production installation is now standardized on `/opt/botman` as the Git checkout, `/opt/botman-venv` for the central process, and `/opt/botman-agent-venv` for the target agent/exporter. `scripts/setup.sh` provisions the supported central/rootless-target host roles and `scripts/update.sh` performs clean fast-forward updates plus venv/unit refresh. Historical log helper/config paths live in central settings (`log_export_bin`, `agent_config_path`), and agent sync uses the same configured target config path.
