# VPS and Discord Setup Guide

This is the operator runbook for installing Botman and getting an application to the point where `/update APP` can deploy it.

## What is runnable now

As of 2026-09-30, the central Discord bot, lifecycle commands, admin onboarding commands, Compose upload/validation, Git deploy-key setup, env management, `/update`, `/livelogs start|stop`, and `/logs tail|download` are implemented and unit-tested. The target `botman-log-agent`, `botman-log-export`, and systemd unit are included. Target OS/package provisioning remains a one-time manual step documented below.

Current automated result: **118 tests passed**. Real VPS Docker/Podman/cysystemd and Discord test-guild acceptance are still required before calling v1 production-complete.

Do not deploy anything under `reference/`.

If you already created a config with the earlier top-level `apps:` layout, this build loads it and nests each app under its recorded stack in memory. The next config mutation/save rewrites the file in the corrected `stacks.<stack>.apps` layout. App names can then be reused in other stacks.

## 1. Host roles

### Central VPS

Runs `botman`. It is the only machine which talks to Git providers and stores:

- `DISCORD_TOKEN`
- `/etc/botman/config.yaml`
- central -> target SSH private keys
- per-app Git deploy private keys
- Git-provider `known_hosts`
- central bare Git caches and temporary release archives

### Remote target VPS

Runs Docker/Podman Compose stacks. It receives release archives by SFTP and does **not** need app Git credentials. Use a dedicated `botmgr` account for Botman management.

### Central VPS also hosting apps

Two safe patterns exist:

1. **Local target:** configure the server as `type: local`. In this case the central `botman` service account itself must own/write `/srv/botman/stacks` and must be able to execute the configured Compose runtime.
2. **Loopback SSH separation:** configure the machine as an SSH target (for example `botmgr@127.0.0.1`) and prepare it exactly like any other remote target.

Do not create `/srv/botman/stacks` owned only by `botmgr` and then configure `type: local`; the local executor runs as `botman`.

## 2. Discord application setup

Create a Discord application/bot and invite it with the `bot` and `applications.commands` OAuth scopes.

Enable **Message Content Intent** because Botman currently supports prefix lifecycle/update commands in addition to slash commands.

In each Botman command channel, grant the bot at least:

- View Channel
- Send Messages
- Read Message History
- Attach Files
- Create Public Threads
- Send Messages in Threads
- Manage Threads
- Manage Webhooks (required for `/livelogs start`)
- Use Application Commands

The configured stack command channel is a management authorization boundary, not just organization. Botman resolves the current channel to exactly one stack, then resolves the app name only inside that stack. The same generic app name may therefore be reused safely in different stack channels.

Record the Discord user IDs which may run admin commands. They go in `ADMIN_IDS` as comma-separated numeric IDs.

## 3. Install the central VPS

Debian/Ubuntu example:

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  git openssh-client ca-certificates

sudo useradd --create-home --shell /bin/bash botman || true
sudo install -d -o botman -g botman -m 0700 /home/botman/.ssh
sudo install -d -o botman -g botman -m 0700 /var/lib/botman
sudo install -d -o botman -g botman -m 0700 /var/lib/botman/repos
sudo install -d -o botman -g botman -m 0700 /var/lib/botman/keys
sudo install -d -o botman -g botman -m 0700 /etc/botman
sudo install -d -o botman -g botman -m 0755 /opt/botman
```

Copy the **fresh repository root** into `/opt/botman`, then install it:

```bash
sudo -u botman python3 -m venv /opt/botman/.venv
sudo -u botman /opt/botman/.venv/bin/pip install --upgrade pip
sudo -u botman /opt/botman/.venv/bin/pip install /opt/botman
```

Create the protected environment file:

```bash
sudo install -o botman -g botman -m 0600 /dev/null /etc/botman/env
sudoedit /etc/botman/env
```

Contents:

```text
DISCORD_TOKEN=replace-with-bot-token
ADMIN_IDS=123456789012345678,234567890123456789
BOTMAN_CONFIG=/etc/botman/config.yaml
```

A config file does **not** have to exist on first start. Botman can start from validated defaults and `/config server add` creates the first persisted config. If `/etc/botman/config.yaml` exists but is invalid, startup fails rather than silently ignoring it.

Install the supplied service:

```bash
sudo cp /opt/botman/systemd/botman.service /etc/systemd/system/botman.service
sudo systemctl daemon-reload
sudo systemctl enable --now botman.service
sudo systemctl status botman.service
journalctl -u botman.service -n 100 --no-pager
```

The supplied unit allows Botman to write `/etc/botman`, `/var/lib/botman`, and `/srv/botman`. If the central host will never be a local target, removing `/srv/botman` from `ReadWritePaths=` is a reasonable hardening step.

## 4. Prepare each remote target VPS

Install OpenSSH and exactly one Compose runtime which you intend Botman to use.

Docker example:

```bash
docker version
docker compose version
```

Podman examples:

```bash
podman --version
podman compose version
```

or, if your design is rootful:

```bash
sudo podman compose version
```

Create the management account and stack root:

```bash
sudo useradd --create-home --shell /bin/bash botmgr || true
sudo install -d -o botmgr -g botmgr -m 0700 /home/botmgr/.ssh
sudo install -d -o botmgr -g botmgr -m 0755 /srv/botman/stacks
```

### Container-runtime privilege choice

There is no privilege-free way to grant arbitrary container lifecycle control:

- Docker-group membership is convenient but effectively root-equivalent.
- `sudo docker ...` / `sudo podman ...` is explicit but still highly privileged.
- Rootless Podman reduces host privilege but must be integration-tested with the journald logging design.

Configure `compose_argv` to match the exact non-interactive command that works as `botmgr`, for example `docker compose`, `podman compose`, or `sudo podman compose`.

If `sudo` is used, verify it is non-interactive:

```bash
sudo -u botmgr sudo -n podman compose version
```

Botman cannot answer a password prompt.

## 5. Central -> target SSH key and host verification

Generate one keypair per target on the central VPS:

```bash
sudo -u botman ssh-keygen \
  -t ed25519 \
  -f /home/botman/.ssh/server-vps1 \
  -N '' \
  -C 'botman central -> vps1'
```

Append `/home/botman/.ssh/server-vps1.pub` to `/home/botmgr/.ssh/authorized_keys` on that target and enforce ownership/mode:

```bash
sudo chown -R botmgr:botmgr /home/botmgr/.ssh
sudo chmod 0700 /home/botmgr/.ssh
sudo chmod 0600 /home/botmgr/.ssh/authorized_keys
```

Botman deliberately uses strict SSH host verification. Obtain the target host-key fingerprint through an independent provider/console channel. You can collect candidates with:

```bash
ssh-keyscan -p 22 vps1.example.com > /tmp/vps1.keys
ssh-keygen -lf /tmp/vps1.keys
```

After independently verifying the fingerprint, append the key to Botman's trusted file:

```bash
sudo -u botman sh -c 'cat /tmp/vps1.keys >> /home/botman/.ssh/known_hosts'
sudo chmod 0600 /home/botman/.ssh/known_hosts
```

Verify the actual Botman identity:

```bash
sudo -u botman ssh \
  -i /home/botman/.ssh/server-vps1 \
  botmgr@vps1.example.com true
```

Never work around a mismatch using `StrictHostKeyChecking=no`.

## 6. Prepare Git-provider trust on central only

Create a separate trust file:

```bash
sudo install -o botman -g botman -m 0600 /dev/null /etc/botman/git_known_hosts
```

Populate it only after checking the provider's SSH host-key fingerprint against the provider's official documentation. Target VPSes do not receive this file and do not receive per-app Git private keys.

## 7. If the central VPS is a `local` target

Prepare `/srv/botman/stacks` for the **botman** account, not `botmgr`:

```bash
sudo install -d -o botman -g botman -m 0755 /srv/botman/stacks
```

Then make the chosen runtime work non-interactively as `botman`, restart `botman.service` so its systemd sandbox picks up the newly created writable path, and use `/config server add server_type:local ...`:

```bash
sudo systemctl restart botman.service
```

If you do not want the Discord service account to have local container privileges, use the loopback-SSH pattern instead.

## 8. Compose requirements

Compose belongs to the **stack**, not the app Git repository. For app `app-a` in stack `bots`, source is activated at:

```text
/srv/botman/stacks/bots/apps/app-a/current
```

The default env path is:

```text
/srv/botman/stacks/bots/env/app-a.env
```

Example service:

```yaml
services:
  app-a:
    build:
      context: ./apps/app-a/current
    env_file:
      - ./env/app-a.env
    restart: unless-stopped
    logging:
      driver: journald
      options:
        tag: botman-bots-app-a
```

For every Botman-managed app, Compose upload checks that:

- the configured service exists
- the build context points at that app's standard `current` release path
- the logging driver is journald
- the journald tag exactly matches the app's `log_identifier`
- the target runtime accepts `compose config`

Persistent application data belongs in named volumes/shared paths outside release directories.

## 9. Runnable Discord onboarding sequence

Admin/config/env commands are slash-only and ephemeral. Run `/config stack add` in the channel which should control the stack.

### Once per target VPS

```text
/config server add
/config server test
```

For SSH, fill in:

- `name`
- `server_type: ssh`
- `host`
- `port`
- `user: botmgr`
- `key: /home/botman/.ssh/server-vps1`
- `known_hosts: /home/botman/.ssh/known_hosts` (recommended explicit path)
- `compose_argv: docker compose` (or your tested equivalent)

For a central/local target use `server_type: local` and the Compose command which works as `botman`.

### Once per stack

In the desired control channel:

```text
/config stack add
```

Provide the stack name and server. `project_name` is optional; Botman derives a stable name if omitted.

### Add the app **before uploading Compose**

```text
/config app add
```

Provide:

- app name (unique only within this stack)
- exact Compose service name
- SSH Git URL (`git@host:owner/repo.git` or `ssh://...`)
- branch
- optional explicit journald `log_identifier`

The stack is inferred from the channel where `/config app add` is run. Adding the app first is important: Compose validation can then verify that app's service/build context/journald tag.

### Correcting configuration after onboarding

Configuration edit commands are slash-only/admin-only/ephemeral. Stack and app edits
resolve the stack from the channel where the command is run:

```text
/config server edit
/config server test
/config stack edit
/config app edit
```

`/config server edit` changes only supplied values. Re-run `/config server test` after
changing SSH or Compose-runtime settings. `/config stack edit` can correct the target
server, Compose project name, or Compose filename only **before apps are added**;
once apps exist Botman rejects those changes because moving an established stack needs
a real filesystem/container migration. `/config app edit APP` can update the Compose
service, repository URL, branch, or journal identifier. Stop live logs before changing
the journal identifier. None of these edits deploy, restart, upload Compose, or rotate
Git keys automatically.

### Upload the shared stack Compose file

```text
/config compose upload
/config compose show
```

Run these commands in the stack command channel; no stack argument is required. `/config compose upload` stages the file, validates it statically, runs the target's configured `compose config`, and atomically replaces the stack Compose file only on success. It does **not** deploy/restart applications.

If you previously uploaded Compose before adding all managed apps, upload it again after app creation so those services are validated.

### Generate the app Git deploy key

```text
/config git setup
```

Run this in the stack command channel. Botman resolves the app inside that stack and returns the public Ed25519 key ephemerally. Add **that public key only** to the repository as a read-only deploy key. The private key is namespaced as `/var/lib/botman/keys/<stack>/<app>` on central.

Key rotation later:

```text
/config git rotate-key
```

Replace the repository's old public deploy key before the next update.

### Configure `.env`

Use any combination:

```text
/env upload APP
/env set APP KEY VALUE
/env unset APP KEY
/env show APP
```

Run `/env ...` in the stack command channel; the app name is resolved only inside that stack. Env writes are atomic and mode `0600`. They never trigger a restart/deploy.

### First deployment

```text
/update APP
```

or:

```text
!update APP
```

The update path:

1. resolves the current command channel to its stack and resolves `APP` only inside that stack
2. fetches the configured branch over strict Git SSH on central
3. resolves the exact commit SHA
4. no-ops if that SHA is already active
5. creates/checksums an archive on central
6. uploads and verifies it on target
7. stages a release and atomically moves `current`
8. builds only the app's Compose service
9. runs `up -d --no-deps` only for that service
10. restores/rolls back on build/activation failure where possible
11. retains the configured number of older releases
12. streams progress into a Discord thread and attaches the complete transcript

### Normal lifecycle

Only from the stack's configured command channel:

```text
/start APP
/stop APP
/restart APP
/status APP
```

Prefix equivalents:

```text
!start APP
!stop APP
!restart APP
!status APP
```

## 10. Target journald/log-agent setup

Deployment works without the log agent, but `/livelogs` and `/logs` require this one-time setup on **every target VPS whose apps should have Discord/journald log access**.

### 10.1 Configure persistent journald

Botman historical downloads read the retained system journal. On each target:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/90-botman.conf >/dev/null <<'EOF2'
[Journal]
Storage=persistent
SystemMaxUse=1G
Compress=yes
EOF2
sudo systemctl restart systemd-journald
journalctl --disk-usage
```

`SystemMaxUse=1G` is a **global VPS journal cap**, not a per-app retention guarantee. If you change `settings.journal_max_use`, keep the host policy aligned manually until provisioning is automated. Review any existing administrator-managed journald policy before installing this drop-in.

### 10.2 Create the agent account and protected config directory

For a normal remote target managed as `botmgr`:

```bash
sudo useradd \
  --system \
  --home-dir /var/lib/botman-log-agent \
  --create-home \
  --shell /usr/sbin/nologin \
  botman-log-agent || true

sudo usermod -aG systemd-journal botman-log-agent
sudo usermod -aG botman-log-agent,systemd-journal botmgr
sudo chown root:botman-log-agent /var/lib/botman-log-agent
sudo chmod 3770 /var/lib/botman-log-agent
sudo install -d -o botman-log-agent -g botman-log-agent -m 0700 /var/lib/botman-log-agent/state
sudo install -d -o root -g root -m 0755 /opt/botman-agent
```

Why `botmgr` gets both groups:

- `botman-log-agent`: lets central atomically replace `/var/lib/botman-log-agent/config.yaml` without broad sudo file-copy permissions;
- `systemd-journal`: lets the short-lived `botman-log-export` helper read retained logs without running the exporter as root.

The management identity already has container lifecycle authority, so treat it as a privileged operational account. New transient SSH sessions pick up the added groups automatically. Verify:

```bash
id botmgr
```

For a **central VPS configured as a local target**, run the same setup but add `botman` instead of `botmgr`:

```bash
sudo usermod -aG botman-log-agent,systemd-journal botman
sudo systemctl restart botman.service
id botman
```

Restarting `botman.service` is required after changing the local service account's supplementary groups. The supplied central systemd unit already permits writes to `/var/lib/botman-log-agent` when that path exists.

### 10.3 Install the agent/exporter package

Copy the same fresh repository/package to `/opt/botman-agent`. On Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip build-essential libsystemd-dev
sudo python3 -m venv /opt/botman-agent/.venv
sudo /opt/botman-agent/.venv/bin/pip install --upgrade pip
sudo /opt/botman-agent/.venv/bin/pip install '/opt/botman-agent[agent]'

/opt/botman-agent/.venv/bin/botman-log-agent --help || true
/opt/botman-agent/.venv/bin/botman-log-export --help
```

`cysystemd` links against systemd; package names differ outside Debian/Ubuntu. Do not substitute a spawned `journalctl` wrapper for the exporter/agent.

Install and enable the supplied agent unit, but do not start it manually before central has written its config:

```bash
sudo cp /opt/botman-agent/systemd/botman-log-agent.service \
  /etc/systemd/system/botman-log-agent.service
sudo systemctl daemon-reload
sudo systemctl enable botman-log-agent.service
```

The generated config path is:

```text
/var/lib/botman-log-agent/config.yaml
```

and per-app checkpoint files are kept under `/var/lib/botman-log-agent/state/` with agent-only `0700`/`0600` permissions. The parent directory is setgid+sticky so the management identity can atomically replace its own config file without gaining read access to checkpoint files.

### 10.4 Allow only the required systemd control commands

Central uses non-interactive `sudo -n` only to restart/query the log-agent service. Find the real systemctl path:

```bash
command -v systemctl
```

Then create `/etc/sudoers.d/botman-log-agent-control` with `visudo -f`. On a typical Debian/Ubuntu host where systemctl is `/usr/bin/systemctl`, a remote target uses:

```sudoers
botmgr ALL=(root) NOPASSWD: /usr/bin/systemctl restart botman-log-agent.service, /usr/bin/systemctl is-active botman-log-agent.service, /usr/bin/systemctl status --no-pager --lines=20 botman-log-agent.service
```

For a central/local target, replace `botmgr` with `botman`. If `command -v systemctl` returns a different path, use that exact path in sudoers.

Validate that sudo is non-interactive. An inactive service may return a nonzero status, which is fine; there must be no password prompt:

```bash
sudo -u botmgr sudo -n systemctl is-active botman-log-agent.service || true
```

Use `sudo -u botman ...` for a local target.

### 10.5 Generate/sync target agent config from Discord

After at least the server/stack/app config exists in Botman:

```text
/config agent sync
/config agent status
```

Choose the target server in each command. `sync` writes the complete server-local agent config, restarts `botman-log-agent.service`, and verifies it is active. At this point apps are present in agent config but live delivery remains disabled until explicitly started.

On the target, useful checks are:

```bash
sudo systemctl status botman-log-agent.service --no-pager
sudo journalctl -u botman-log-agent.service -n 100 --no-pager
sudo -u botmgr /opt/botman-agent/.venv/bin/botman-log-export \
  --config /var/lib/botman-log-agent/config.yaml \
  --app STACK.APP \
  --tail 5 \
  --format human \
  --stdout
```

The agent/exporter uses an internal stack-qualified key (`STACK.APP`) so repeated app names on one VPS cannot collide. The last command verifies the management account can read that app's journald tag. Replace `botmgr` with `botman` on a local target.

### 10.6 Start/stop live logs from the app's command channel

```text
/livelogs start APP
/livelogs stop APP
```

Prefix equivalents:

```text
!livelogs start APP
!livelogs stop APP
```

`start` is idempotent/repairing: it reuses a healthy thread/webhook where possible, reopens an archived reusable thread, or creates a replacement when the saved destination is unusable. Central then rewrites the whole target-agent config and restarts the agent. `stop` disables that app in agent config and best-effort archives its live thread.

### 10.7 Read retained history

Recent retained entries:

```text
/logs tail APP lines:100
!logs tail APP 100
```

Range download in the configured Botman timezone (default `Europe/Istanbul`):

```text
/logs download APP from_time:"2026-09-30 10:00" to_time:"2026-09-30 12:00" format:human
/logs download APP from_time:"2026-09-30 10:00" to_time:"2026-09-30 12:00" format:jsonl
```

Prefix form:

```text
!logs download APP "2026-09-30 10:00" "2026-09-30 12:00" human
```

The central bot converts local wall times through the configured IANA timezone, rejects nonexistent/ambiguous DST wall times, asks the fixed target helper for gzip parts under the Discord upload budget, downloads those parts, and removes the target temp directory. Historical exports preserve full journal messages; Discord-only live truncation does not modify retained history.

`/logs tail` currently scans the retained entries matching that app tag with bounded memory and returns the newest requested entries. It is suitable for convenience tails; use a bounded `/logs download` time range for larger investigations.

## 11. App-ready checklist

For a deployable app, all of these should be true:

1. Target runtime works non-interactively as its Botman management identity.
2. Central SSH host verification succeeds for that target.
3. `/config server test` passes.
4. `/config stack add` was run in the intended command channel.
5. `/config app add` was run in that stack channel before final Compose validation.
6. `/config compose upload` succeeds against the target runtime.
7. `/config git setup` public key is installed read-only in the repository.
8. Git-provider host key exists in `/etc/botman/git_known_hosts` on central.
9. Required env values are present.
10. `/update APP` succeeds.
11. `/status APP` reports the expected service state.

If Discord/journald logging is required, also verify:

12. The one-time target log-agent package/systemd/group/sudoers setup is complete.
13. `/config agent sync` and `/config agent status` succeed for the target server.
14. The app's Compose journald tag is visible to `botman-log-export`.
15. `/livelogs start APP` delivers new journal entries.
16. `/logs tail APP` and a small `/logs download` range both work.

These logging steps are implemented, but real-host cysystemd/Discord acceptance is still required before production sign-off.

## 12. Secret/location matrix

| Item | Central | Target |
|---|---:|---:|
| Discord bot token | yes | no |
| App Git deploy private key | yes | no |
| Git-provider trusted host keys | yes | no |
| Central -> target SSH private key | yes | no |
| Central SSH public key | copy | `botmgr` authorized_keys |
| Botman-managed Compose file | source/transit | yes |
| App release source | archive/cache | yes |
| App `.env` | sent/managed | yes |
| Git checkout / `.git` | bare cache | no |
| Log-agent webhook secret | central config when enabled | agent config while enabled |
