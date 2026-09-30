# HANDOFF — Botman Fresh Start

> Keep this file concise/current. Detailed rationale belongs in the linked docs.

## Current status

- No production deployment exists; backward compatibility is not required.
- Archived code under `reference/original-ag-botman/` is reference-only.
- Fresh implementation is active.
- Current automated suite: **127 passed**.
- Phase 1 is complete.
- Phase 2 central Compose/lifecycle/Discord core is complete at unit level.
- Phase 3 central Git + `/update` deployment core and Discord adapter are complete at unit level.
- Phase 4 live log-agent core/runtime is implemented and unit-tested with fake journal/HTTP adapters.
- Phase 5 central live-log control is implemented; one-time target agent OS/package provisioning remains manual and documented.
- Phase 6 historical exporter plus `/logs tail|download` is implemented and unit-tested.
- Phase 7 env/config onboarding, including conservative server/stack/app edit UX, is implemented.
- Phase 8 systemd packaging/docs are in progress; central and agent units exist.

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
- Env/config changes do not implicitly restart/deploy apps.
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
- `/config git setup|rotate-key`
- `/config agent sync|status`
- `/env show|upload|set|unset`

See `VPS_AND_DISCORD_SETUP.md` for the exact onboarding order.

## Immediate next work

1. Run actual Linux/cysystemd integration for the live reader and historical exporter, including retained-history and journal-permission checks.
2. Run real Docker and Podman Compose deployment/lifecycle/failure acceptance.
3. Run a Discord test-guild acceptance pass for hybrid command registration, threads, webhooks, upload limits, and archived/locked thread behavior.
4. Decide whether to automate target agent provisioning after real-host package/systemd behavior is verified; the manual runbook is the supported bootstrap today.
5. Reconcile runtime-specific findings before v1 completion.

Do **not** repair or reuse archived `py/agent/log_agent.py` or deployment code as the implementation baseline.

## Handoff maintenance rules

- Update this file after each completed phase/material decision.
- Keep `DECISIONS.md` authoritative for approved choices.
- Put detailed rationale in subsystem docs.
- Record deferred work in `ROADMAP.md`.
- Do not call v1 complete until `TEST_PLAN.md` real integration/failure drills pass.
