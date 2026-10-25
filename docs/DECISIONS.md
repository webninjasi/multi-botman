# Agreed Decisions

This file records the decisions explicitly settled with the user for the fresh implementation.

## System shape

- One central Python Discord bot.
- One lightweight logging daemon per VPS, including the central VPS if it hosts managed apps.
- No exposed management HTTP/API ports on target VPSes.
- Central -> VPS management is transient SSH/SFTP.
- Logging daemon handles logging only; it does not deploy code, run Git, or act as a generic remote management agent.

## Stack/application model

- Multiple apps may share one Compose file and one Discord category/command channel.
- Each app has its own Git repository.
- Introduce a first-class **stack** object:
  - target server
  - Discord command channel
  - stable Compose project name
  - shared Compose file/location
  - zero or more managed apps/services
- App names are unique only within a stack; different stacks may reuse generic names such as `app`, `db`, and `worker`.
- App commands select the app within the stack resolved from the current command channel, never the VPS and never by a global app lookup.
- The stack command channel is the app-management security boundary. An unconfigured channel is rejected. Supporting lifecycle commands from threads is not required for v1.

## Compose and filesystem

- Compose YAML is configured/uploaded via Botman, not taken from each app repo.
- Managed app source is placed under a standard Botman-controlled stack layout; arbitrary deploy paths should not be necessary.
- Persistent application data must not live in a release/code directory.
- Named volumes are acceptable; bind mounts to explicit shared paths outside release directories are only acceptable when deliberately configured.

## Git/deployment

- Git credentials live only on the central VPS.
- Each stack-local app uses its own SSH deploy key stored on the central VPS; key/cache paths are namespaced by stack.
- Target VPSes do not receive Git deploy keys/PATs and do not need Git provider SSH setup.
- Because central uses Git-over-SSH, the central bot user still needs normal strict SSH host verification for the Git provider (`known_hosts`). Do not hard-code GitHub host keys in application source.
- V1 deployment trigger: explicit `/update` / `!update` only.
- Future push-triggered deployment stays in roadmap. Planned design: known Discord webhook identity + timestamped canonical payload + HMAC signature + replay window. Never place the secret itself in the Discord message.
- Central fetches/resolves the configured branch, compares the remote active commit, and no-ops when already deployed unless a future force option is explicitly added.
- Deployment ships a source archive to the VPS, stages a release, builds, switches/restarts safely, retains rollback material, and cleans old releases.
- Deployment progress goes to a thread created under the stack command channel. Full transcript is attached at completion.

## Logging

- Use `cysystemd` 2.x and direct libsystemd journal access; no `journalctl` subprocess.
- Use stable `SYSLOG_IDENTIFIER` values set through the container journald logging `tag` option.
- Live logs stay active until explicitly stopped.
- `/livelogs start APP` and `/livelogs stop APP` are explicit commands; no toggle semantics.
- `/livelogs start` also repairs an already-enabled subscription if the existing thread is archived/unusable/deleted:
  - reuse/unarchive when possible;
  - otherwise create a replacement thread;
  - update agent config and restart the logging daemon on that VPS.
- If webhook delivery receives a clearly fatal destination error, the agent suspends that app's live stream rather than hot-looping. Central repairs it when `/livelogs start` is run again.
- After a long outage, do not flood Discord with the entire missed journal backlog. Historical gaps are retrieved with `/logs download`.
- `SYSLOG_IDENTIFIER` is accepted as the app log key.

## Discord log formatting

Preserve log order.

Priority when a journal entry does not fit the remaining Discord body budget:

1. Flush the current message at an entry boundary when possible.
2. For a multi-line entry that itself exceeds one Discord message, split it across multiple Discord messages **between logical lines**. Do not throw away remaining lines merely because the whole journal entry did not fit in one message.
3. If a single logical line itself is too long, send that line alone and truncate its characters to fit, with an explicit truncation marker and original length when available.
4. Continue with subsequent lines/entries in order.
5. Do not advance that journal entry's checkpoint until every Discord message representing that entry has been acknowledged.

Historical downloads preserve the original journald message and therefore compensate for Discord-only truncation.

## Historical logs

- Keep a `/logs` recent/tail command.
- Add `/logs download` for a user-specified date/time range.
- Date input is interpreted in a configurable timezone, default `Europe/Istanbul`; convert to UTC internally.
- Export formats:
  - `human` (default): compressed human-readable `.log.gz`.
  - `jsonl`: compressed structured `.jsonl.gz`.
- Large exports are split into multiple Discord attachments rather than rejected solely for size.
- This is not permanent archival. Availability is bounded by journald retention.
- Configure/verify persistent journald storage with a 1 GiB total journal cap per managed VPS until an archival sink exists.

## Discord commands

- Prefix commands are useful and should not be removed just to simplify implementation.
- Lifecycle/user commands should use discord.py hybrid commands/groups where the UX remains clean.
- Admin/config/env commands stay slash-only where ephemeral replies, attachments, and structured options matter.
- `.env` modal editing is removed. Keep show/upload/set/unset.

## Agent reload behavior

- Hot config reload is not needed in v1.
- Central may rewrite `/var/lib/botman-log-agent/config.yaml` and `systemctl restart` the agent for live-log config changes.
- A brief interruption to other live streams on that VPS is acceptable; checkpoint/resume policy handles short restarts.
