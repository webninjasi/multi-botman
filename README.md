# Botman

Botman is a Discord-managed control plane for Docker/Podman Compose applications across small local/remote VPSes. The central bot performs management and deployment over transient SSH/SFTP; a separate tiny per-VPS daemon will handle live journald -> Discord log delivery only.

## Current implementation status

Fresh implementation started on 2026-09-30. The archived prototype under `reference/` is reference-only and must not be deployed or patched forward as the production implementation.

Implemented now:

- strict Pydantic configuration models for settings, servers, stacks, apps, Git, and live-log state
- unknown-key/reference/name/path/timezone validation
- atomic YAML persistence with `0600` permissions and serialized async mutation
- app -> stack -> server resolution and command-channel authorization
- argv-safe local subprocess execution with timeout and line streaming
- transient AsyncSSH executor design with strict host verification, public-key-only auth, SFTP helpers, and checked privileged writes
- stack-scoped Compose argv construction with stable project/file identity
- per-stack operation locking
- service-scoped start/stop/restart/status/build/up core
- Compose YAML size/syntax/service/build-context/journald-tag validation

The automated fresh-start suite currently passes **36 tests**.

Not implemented yet:

- Discord bot entrypoint/cogs and actual slash/prefix registration
- Compose upload transport/runtime `compose config` validation
- Git cache and `/update` deployment engine
- cysystemd log agent, live log controls, and historical exporter
- admin env/config command adapters
- systemd packaging/production deployment

## Operator setup

Read [`VPS_AND_DISCORD_SETUP.md`](VPS_AND_DISCORD_SETUP.md) for:

- central VPS preparation
- every target VPS preparation
- SSH host-key verification and key placement
- container runtime privilege tradeoffs
- journald preparation
- required Compose layout/logging tags
- the exact planned Discord command sequence for onboarding and operating an app

A model configuration example is in [`config.example.yaml`](config.example.yaml).

## Development

Python 3.11+ is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

The logging-agent extra will later be installable with:

```bash
pip install -e '.[agent]'
```

## Documentation read order

1. `HANDOFF.md` — current implementation state and next work.
2. `VPS_AND_DISCORD_SETUP.md` — operator bootstrap/onboarding runbook.
3. `DECISIONS.md` — settled product/architecture decisions.
4. `ARCHITECTURE.md` — system and data flows.
5. `IMPLEMENTATION_PLAN.md` — phased implementation sequence.
6. `CONFIG_AND_COMMANDS.md` — config model and command UX.
7. `SECURITY.md` — trust boundaries and security requirements.
8. `TEST_PLAN.md` — acceptance and integration requirements.
9. `LOGGING.md` / `DEPLOYMENT.md` — detailed subsystems.
10. `ROADMAP.md`, `REFERENCES.md`, `HISTORY.md`.
