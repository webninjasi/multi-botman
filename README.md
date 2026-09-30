# Botman

Botman is a Discord-managed control plane for Docker/Podman Compose applications across small local or remote VPSes. The central bot owns management/deployment and talks to targets through transient SSH/SFTP. A separate tiny per-VPS agent reads journald directly for live Discord logs and provides a short-lived historical export helper.

## Current implementation status

Fresh implementation started on 2026-09-30. The archived prototype under `reference/` is reference-only and must not be deployed or patched forward.

Implemented and covered by the current automated suite:

- strict Pydantic configuration and atomic `0600` persistence
- channel -> stack -> stack-local app -> server routing and command-channel authorization
- argv-safe local and strictly host-verified SSH/SFTP execution
- stack-scoped Compose identity, per-stack locks, service-only lifecycle operations
- runtime-affecting config/Git/env mutations share the same stack operation lock
- hybrid Discord `/start|stop|restart|status` + prefix equivalents
- slash-only admin onboarding for servers, stacks, apps, Compose, Git keys, and agent sync/status; slash-only ephemeral `/env` uses the same stack-channel authorization as `/update`
- runtime `compose config` validation before atomic Compose activation
- central Git-over-SSH cache with strict host verification and stack-namespaced per-app deploy keys
- `/update` + `!update` release staging, checksum verification, atomic `current`, rollback, pruning, deployment thread, and complete transcript attachments
- target `botman-log-agent`: cysystemd 2.x direct async journal reading, bounded/backpressured webhook delivery, line-aware Discord formatting, atomic checkpoints, restart/gap policy, and per-app supervision
- persistent `/livelogs start|stop` with webhook/thread repair, server-scoped synchronization, and rollback on target-agent restart failure
- `botman-log-export` plus `/logs tail|download` for retained journald history in human or JSONL gzip parts
- central and agent systemd unit files with restrictive default umasks
- clean-checkout CI for Python 3.11-3.13, Ruff/mypy quality gates, and wheel-content/entry-point verification
- target-host preflight CLI for Compose, journald policy/permissions, cysystemd, systemd unit/sudoers, and optional app export checks

The automated suite currently passes **174 tests**.

Still required before calling v1 production-complete:

- real-host verification of the automated/manual target bootstrap across supported distributions
- real Linux/cysystemd+journald acceptance
- real Docker and Podman integration/failure drills on VPSes
- end-to-end Discord test-guild acceptance

## Operator setup

The canonical production layout is a Git checkout plus external virtualenvs:

```text
/opt/botman/                 Git checkout
/opt/botman-venv/            central control-plane venv
/opt/botman-agent-venv/      target agent/exporter venv
/etc/botman/                 central config/secrets
/var/lib/botman-log-agent/   protected target agent config/state
/srv/botman/stacks/          managed application stacks
```

Bootstrap the checkout first if the host does not have it yet:

```bash
sudo git clone https://github.com/webninjasi/multi-botman.git /opt/botman
```

For a central VPS that also hosts rootless Podman apps, run:

```bash
sudo /opt/botman/scripts/setup.sh --mode all
```

For a target-only VPS:

```bash
sudo /opt/botman/scripts/setup.sh --mode target
```

Target setup auto-detects an already-working `docker compose` or `podman compose`. Existing Docker is kept and `botmgr` is granted access to that daemon; existing Podman is configured rootlessly with its persistent user socket. If both runtimes are usable, select one explicitly with `--runtime docker` or `--runtime podman`. The script never installs Podman merely because target mode was requested.

The setup script is idempotent for the supported layout. It installs host prerequisites, users/directories, external venvs, systemd units, persistent journald policy, and the restricted agent-control sudoers rule. It deliberately does **not** invent Discord secrets, SSH trust, or Git deploy keys.

After code is pushed to the configured branch, update an installed host with:

```bash
sudo /opt/botman/scripts/update.sh
```

The updater refuses tracked local changes, fast-forwards Git only, reinstalls installed central/agent components, refreshes the shipped unit files, reloads systemd, and restarts configured services.

Read [`VPS_AND_DISCORD_SETUP.md`](VPS_AND_DISCORD_SETUP.md) for first-time trust/onboarding, manual alternatives, Discord commands, and migration details. It distinguishes:

- central VPS setup
- remote target VPS setup
- the special case where the central VPS is also a local target
- SSH and Git host-key trust
- Docker/Podman privilege choices
- Compose/journald requirements
- the runnable Discord onboarding/deployment command sequence
- automated rootless-Podman target bootstrap plus manual alternatives, followed by Discord-controlled live/historical logging

A model configuration example is in [`config.example.yaml`](config.example.yaml).

### Config namespace note

Apps are nested under `stacks.<stack>.apps` and their names are only unique inside that stack. Runtime commands always resolve the current Discord channel to a stack before resolving `APP`. Existing config files from the earlier flat `apps:` implementation are accepted as a one-way migration; the next Botman config save writes the corrected nested form.

## Development

Python 3.11+ is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,agent]'
python -m ruff check src tests scripts
python -m mypy src
pytest -q
python -m compileall -q src scripts
```

Executables:

```bash
botman
botman-log-agent
botman-log-export --help
botman-target-preflight --help
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
