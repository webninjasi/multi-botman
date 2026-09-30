# HANDOFF — Botman Fresh Start

> Keep this file concise and current. Detailed rationale belongs in the linked docs. Update this file whenever architecture, restrictions, current phase, or next TODOs change.

## Current status

- No production deployment exists. Backward compatibility is **not required**.
- The old `py/` implementation is archived under `reference/original-ag-botman/` and is reference-only.
- Fresh architecture is agreed; implementation has started.
- Phase 1 is complete and the fresh test suite passes. Phase 2 core Compose/locking/validation work is in progress.
- The archived test suite reported **11 passed / 2 errors**; see `reference/REVIEW_FINDINGS.md`. Do not treat those tests as acceptance tests for the fresh implementation.

## Product goal

One central Discord bot manages Docker/Podman Compose applications across small local/remote VPSes using transient SSH/SFTP. One tiny per-VPS daemon handles **live journald -> Discord log delivery only**. No management web ports, no persistent SSH log streams, no DB/Redis/Loki/Grafana/Portainer-class infrastructure.

## Non-negotiable decisions

Read `DECISIONS.md` for detail. In short:

- Central bot is the only management/control plane.
- Remote management uses transient SSH/SFTP with strict host verification.
- Lifecycle commands remain available as hybrid slash/prefix commands where practical; admin/secret commands remain slash-only/ephemeral.
- Discord command channel is the authorization/routing boundary for apps in that stack. Do not add a second app ACL system.
- Model a **stack** separately from an **app**: one stack = one server + one command channel + one Compose project/file; multiple apps can share it.
- Compose YAML is managed/uploaded through Botman, not stored in each app repo.
- Each app has its own Git repo and central-VPS SSH deploy key.
- Only the central VPS talks to Git. Target VPSes never need GitHub/GitLab credentials or Git host configuration.
- `/update` is the only deployment trigger in v1. Future push-triggered deployment stays TODO and must use webhook identity + HMAC signature, never a plaintext shared secret in Discord.
- Deployment uses source archives + staged release directories + atomic `current` symlink switching; no persistent data in code/release directories. Named volumes are acceptable.
- The log daemon uses **cysystemd 2.x `AsyncJournalReader`**, never a spawned `journalctl` process.
- App logs are identified by a stable per-app `SYSLOG_IDENTIFIER` produced by Compose journald `tag` configuration.
- Live logs stay enabled until explicit `/livelogs stop`.
- `/livelogs start` is idempotent and also repairs/replaces an unusable thread.
- Live log delivery may skip long outage gaps; `/logs download` is the recovery path. Avoid flooding Discord after a long agent/thread outage.
- Live Discord formatting preserves order, splits multi-line entries at line boundaries, and only truncates a logical line when that single line itself cannot fit.
- Historical export returns original journald content and supports human `.log.gz` and structured `.jsonl.gz`.
- Default display timezone is `Europe/Istanbul`; internal timestamps are UTC.
- Journald retention target is persistent storage capped at **1 GiB per VPS** until external archival exists.
- No `.env` modal editor. Keep show/upload/set/unset; all secret-bearing responses are ephemeral. `.env` writes use restrictive permissions.
- Config/env changes never implicitly deploy/rebuild applications. Agent config may be restarted when logging configuration changes; hot reload is out of scope for v1.
- `/update` creates a deployment thread under the stack's command channel, streams concise progress there, and attaches the complete transcript at the end.

## Immediate next work

Continue Phase 2 in `IMPLEMENTATION_PLAN.md`:

1. Wire `LifecycleService` into discord.py hybrid `/start`, `/stop`, `/restart`, and `/status` adapters; both slash and prefix paths must call the same authorization boundary.
2. Implement admin Compose upload/show transport with safe SFTP/local atomic writes and runtime `compose config` validation.
3. Add Discord output/transcript helpers needed by lifecycle/deployment commands.
4. Finish Phase 2 acceptance tests, including actual Discord adapter routing tests.
5. Only after Phase 2 passes, begin Phase 3 central Git cache and `/update`.

Current fresh suite: **36 passed**. See `VPS_AND_DISCORD_SETUP.md` for the operator bootstrap and planned Discord onboarding sequence.

Do **not** repair or reuse the archived `py/agent/log_agent.py` or `deployment.py` as the implementation baseline.

## Handoff maintenance rules

- Update `HANDOFF.md` after every completed phase or material decision.
- Put rationale/long explanations in the detailed docs and link them here.
- Record completed architectural pivots in `HISTORY.md`.
- Record new/deferred work in `ROADMAP.md`; do not silently scope-creep v1.
- Keep `DECISIONS.md` authoritative for user-approved choices.
- Never copy old behavior just for compatibility; this project is not deployed.
- Do not mark a phase complete until its acceptance tests in `TEST_PLAN.md` pass.
- When implementation disagrees with docs, stop and reconcile the docs/decision before proceeding.
