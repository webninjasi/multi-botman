# VPS and Discord Setup Guide

This is the operator runbook for preparing Botman hosts and onboarding an app.

## Implementation status

As of 2026-09-30:

- Phase 1 is implemented and tested: configuration models/persistence, routing authorization, local/SSH execution, SFTP primitives.
- Phase 2 core is in progress: Compose argv construction, stack locking, lifecycle service core, and Compose YAML validation are implemented and tested.
- The Discord bot entrypoint/admin command adapters, deployment engine, and log agent are **not yet runnable**. Commands below describe the agreed v1 operator workflow and are the interface the remaining phases will implement.

Do not deploy the archived code under `reference/`.

## Roles

### Central VPS

Runs the Discord bot and is the only machine which talks to Git providers. It holds:

- `DISCORD_TOKEN`
- `/etc/botman/config.yaml`
- central -> target VPS SSH private keys
- per-app Git deploy private keys
- Git-provider `known_hosts`
- Git mirrors/cache and generated release archives

If the central VPS also hosts applications, prepare it as both **central** and **target**.

### Target VPS

Runs one or more Docker/Podman Compose stacks. It receives source release archives from the central VPS over SFTP. It does **not** need GitHub/GitLab credentials.

Each target eventually also runs the small `botman-log-agent`, which only reads journald and posts enabled live logs to Discord.

## 1. Discord setup

Create a Discord application/bot in the Discord Developer Portal. Invite it to the server with the `bot` and `applications.commands` OAuth scopes.

Because v1 keeps prefix commands such as `!status app-a`, enable the **Message Content Intent** for the bot.

Grant the bot, in the Botman command channels, the permissions needed for:

- View Channel
- Send Messages
- Read Message History
- Attach Files
- Create Public Threads
- Send Messages in Threads
- Manage Threads
- Manage Webhooks
- Use Application Commands

Restrict the command channel itself with normal Discord channel permissions. Botman treats the configured stack command channel as the application-management authorization boundary.

Record your own Discord user ID for `ADMIN_IDS`. Multiple admin IDs are comma-separated; whitespace is ignored.

## 2. Central VPS bootstrap

The examples below assume Debian/Ubuntu. Adjust package names for another distribution.

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  git openssh-client ca-certificates

sudo useradd --create-home --shell /bin/bash botman || true
sudo install -d -o botman -g botman -m 0700 /home/botman/.ssh
sudo install -d -o botman -g botman -m 0700 /var/lib/botman
sudo install -d -o botman -g botman -m 0700 /etc/botman
```

Copy this repository to a stable location such as `/opt/botman` and create a virtual environment:

```bash
sudo install -d -o botman -g botman -m 0755 /opt/botman
# copy/rsync repository contents into /opt/botman first
sudo -u botman python3 -m venv /opt/botman/.venv
sudo -u botman /opt/botman/.venv/bin/pip install --upgrade pip
sudo -u botman /opt/botman/.venv/bin/pip install /opt/botman
```

The central service entrypoint/systemd unit is intentionally deferred until the bot command layer exists. Do not use the archived service unit as a production substitute.

Create the protected central environment file now if desired:

```bash
sudo install -o botman -g botman -m 0600 /dev/null /etc/botman/env
sudoedit /etc/botman/env
```

Planned contents:

```text
DISCORD_TOKEN=replace-with-bot-token
ADMIN_IDS=123456789012345678,234567890123456789
BOTMAN_CONFIG=/etc/botman/config.yaml
```

## 3. Generate one central -> target SSH key per VPS

On the central VPS, as the `botman` user:

```bash
sudo -u botman ssh-keygen \
  -t ed25519 \
  -f /home/botman/.ssh/server-vps1 \
  -N '' \
  -C 'botman central -> vps1'
```

Repeat with a different filename for every target VPS.

Do not copy application Git deploy keys to target VPSes.

## 4. Prepare every target VPS

Install SSH plus exactly one supported container/Compose runtime.

### Docker example

Install Docker Engine and the Compose plugin using your distribution/vendor procedure, then verify:

```bash
docker version
docker compose version
```

### Podman example

Install Podman and a Compose provider, then verify the exact command you plan to configure, for example:

```bash
podman --version
podman compose version
```

or, if using rootful Podman:

```bash
sudo podman compose version
```

### Management account

Create a dedicated account:

```bash
sudo useradd --create-home --shell /bin/bash botmgr || true
sudo install -d -o botmgr -g botmgr -m 0700 /home/botmgr/.ssh
sudo install -d -o botmgr -g botmgr -m 0755 /srv/botman/stacks
```

Append the corresponding central public key to the target:

```bash
sudoedit /home/botmgr/.ssh/authorized_keys
sudo chown botmgr:botmgr /home/botmgr/.ssh/authorized_keys
sudo chmod 0600 /home/botmgr/.ssh/authorized_keys
```

### Runtime privilege choice

Choose deliberately:

- **Docker group:** convenient, but membership in the Docker group is effectively root-equivalent on that VPS.
- **`sudo podman` / `sudo docker`:** also gives the management path substantial privilege; command-path restrictions do not make arbitrary container control non-privileged.
- **Rootless Podman:** reduces host privilege, but must be integration-tested with the chosen journald/logging setup before treating it as the default.

Botman should still use a dedicated account even where the container runtime itself is highly privileged.

Configure `compose_argv` to match what actually works as `botmgr`; examples:

```yaml
compose_argv: [docker, compose]
```

```yaml
compose_argv: [sudo, podman, compose]
```

If `sudo` is required, give `botmgr` only the non-interactive privileges needed by the selected runtime and later Botman provisioning. Verify with:

```bash
sudo -u botmgr sudo -n podman compose version
```

(or the Docker equivalent). Botman cannot answer an interactive sudo password prompt.

## 5. Verify SSH host identity from the central VPS

Botman requires strict host-key verification. Never solve SSH errors with `StrictHostKeyChecking=no`.

Obtain the target's SSH host-key fingerprint through an independent source such as the VPS provider console or direct console access. You can collect candidate public keys with:

```bash
ssh-keyscan -p 22 vps1.example.com > /tmp/vps1.keys
ssh-keygen -lf /tmp/vps1.keys
```

Compare the displayed fingerprint out-of-band. Only after it matches, install the key into the central `botman` account's trusted hosts file:

```bash
sudo -u botman sh -c 'cat /tmp/vps1.keys >> /home/botman/.ssh/known_hosts'
sudo chown botman:botman /home/botman/.ssh/known_hosts
sudo chmod 0600 /home/botman/.ssh/known_hosts
```

Then test the exact key/account:

```bash
sudo -u botman ssh \
  -i /home/botman/.ssh/server-vps1 \
  botmgr@vps1.example.com true
```

Repeat for every target.

## 6. Prepare Git-provider host verification on the central VPS

Only the central VPS talks to GitHub/GitLab/etc.

Create a separate trusted-hosts file if you want Git trust isolated from VPS trust:

```bash
sudo install -o botman -g botman -m 0600 /dev/null /etc/botman/git_known_hosts
```

Populate it using host keys whose fingerprints you verify against the Git provider's official published fingerprints. Do not automatically trust raw `ssh-keyscan` output and do not hard-code provider keys into Botman source.

Each app will later get a separate **read-only** Git deploy key from `/config git setup` or `/config git rotate-key`.

## 7. Journald preparation on every target

Phase 5 will automate and verify this, but a target can be prepared manually now.

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/90-botman.conf >/dev/null <<'EOF2'
[Journal]
Storage=persistent
SystemMaxUse=1G
EOF2
sudo systemctl restart systemd-journald
journalctl --disk-usage
```

Create the future log-agent account and journal permission:

```bash
sudo useradd \
  --system \
  --home-dir /var/lib/botman-agent \
  --create-home \
  --shell /usr/sbin/nologin \
  botman-log || true
sudo usermod -aG systemd-journal botman-log
sudo install -d -o root -g botman-log -m 0750 /etc/botman-agent
sudo install -d -o botman-log -g botman-log -m 0750 /var/lib/botman-agent
```

The actual `botman-log-agent` package/service is not implemented yet; Phase 5 will install and test it.

## 8. Compose file requirements for a managed app

Compose belongs to the **stack**, not to the app Git repository.

For an app named `app-a` in stack `bots`, Botman expects source at:

```text
/srv/botman/stacks/bots/apps/app-a/current
```

and the default env file at:

```text
/srv/botman/stacks/bots/env/app-a.env
```

A minimal managed service should look like:

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

`logging.options.tag` must match the app's configured `log_identifier` exactly. Put persistent data in named volumes or explicit shared paths outside `apps/<app>/releases/`; never put persistent state inside a release directory.

Multiple independently versioned app services may share this same Compose file, network, and named volumes.

## 9. Discord command sequence to onboard an app

These are the agreed v1 commands. Admin/config/env commands are slash-only and ephemeral. Lifecycle commands are hybrid slash/prefix where practical.

### One-time server setup

```text
/config server add
/config server test
```

For an SSH server, provide the server name, host, port, user, central SSH key path, optional explicit `known_hosts` path, and Compose argv.

When the log-agent implementation lands:

```text
/config agent provision
/config agent status
```

Run provisioning for every VPS which hosts managed apps, including the central VPS if it is also an app host.

### One-time stack setup

Run stack creation **inside the Discord channel which should control that stack**:

```text
/config stack add
```

Provide the stack name, server, and stable Compose project name. The current channel becomes the stack command channel.

Upload the shared Compose file:

```text
/config compose upload
/config compose show
```

A Compose upload changes configuration only; it does not restart or deploy apps.

### Add an app

```text
/config app add
```

Provide at minimum:

- app name
- stack
- Compose service name
- Git repo SSH URL
- branch
- stable journald `log_identifier`

Then create the app-specific Git deploy key:

```text
/config git setup
```

Botman will show the **public** key ephemerally. Add that public key to the app repository as a read-only deploy key. The private key stays only on the central VPS.

Use this later to replace the key:

```text
/config git rotate-key
```

### Configure environment

Choose one or more:

```text
/env upload APP
/env set APP KEY VALUE
/env unset APP KEY
/env show APP
```

Environment changes never automatically deploy or restart the app.

### First deployment

After the Compose service, Git deploy key, and env are ready:

```text
/update APP
```

or:

```text
!update APP
```

`/update` is the only deployment trigger in v1. It will create a deployment thread, fetch/resolve the app repo on the central VPS, transfer a source archive, build only that service, activate it, and attach the full transcript.

### Normal lifecycle

From the stack's configured command channel only:

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

Typing a known app name from another channel is intentionally rejected.

### Live and historical logs

Enable/repair live logs:

```text
/livelogs start APP
```

Stop them explicitly:

```text
/livelogs stop APP
```

Prefix equivalents are planned:

```text
!livelogs start APP
!livelogs stop APP
```

Recent logs:

```text
/logs tail APP
/logs tail APP lines:100
```

Convenient prefix fallback:

```text
!logs APP 100
```

Historical export, interpreting entered times in the configured display timezone (default `Europe/Istanbul`):

```text
/logs download APP from:"2026-09-30 10:00" to:"2026-09-30 12:00" format:human
/logs download APP from:"2026-09-30 10:00" to:"2026-09-30 12:00" format:jsonl
```

## 10. Recommended onboarding order

For each new app, use this order:

1. Prepare/verify its target VPS and central SSH trust.
2. `/config server add` and `/config server test` (once per VPS).
3. `/config stack add` in the intended command channel (once per stack).
4. `/config app add`.
5. `/config compose upload` with all managed services represented correctly.
6. `/config git setup`, then add the shown public key to the repository as read-only.
7. `/env upload` and/or `/env set`.
8. `/config agent provision` once Phase 5 is implemented.
9. `/update APP`.
10. `/status APP`.
11. `/livelogs start APP` if desired.
12. Verify `/logs tail APP` and one `/logs download` range.

## What should exist where

| Item | Central VPS | Target VPS |
|---|---:|---:|
| Discord token | yes | no |
| App Git deploy private key | yes | no |
| Git provider known_hosts | yes | no |
| Central -> target SSH private key | yes | no |
| Corresponding SSH public key | local copy | `authorized_keys` |
| Compose file | managed copy/transit | yes |
| App source releases | temporary archive/cache | yes |
| App `.env` | managed over SSH | yes |
| Git checkout / `.git` | mirror/cache only | no |
| `botman-log-agent` | only if central hosts apps | yes |
| Discord log webhook secret | central config; agent config when live | only active logging config |

