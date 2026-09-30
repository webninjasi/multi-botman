# Logging Design

## Goals

- Very low RAM/CPU on tiny VPSes.
- No persistent SSH log streams.
- No spawned `journalctl` process.
- Preserve live log order.
- Avoid unbounded Python queues/buffers during Discord outages.
- Live Discord is presentation, not archival.
- Full retained history remains downloadable from journald.

## Library

Use `cysystemd` 2.x, specifically `cysystemd.async_reader.AsyncJournalReader`.

Implementation expectations:

- open the system journal (`JournalOpenMode.SYSTEM`)
- filter with `Rule("SYSLOG_IDENTIFIER", app_identifier)`
- use `JournalEntry.cursor` and realtime timestamp metadata
- use `seek_cursor()` for short restart resume
- use `seek_tail()` for new subscriptions or when abandoning a large gap
- for historical export use realtime seeking and stop at the requested end timestamp
- set journal data threshold deliberately; historical export must retrieve complete message fields (`data_threshold = 0`). Live-reader threshold behavior must be tested before choosing any nonzero cap.

Current cysystemd 2.x has direct async iteration rather than the old internal queue/thread implementation. Do not implement a second queue unless profiling proves it necessary.

## Compose logging convention

Each managed app service must write to journald with a stable unique tag, e.g.:

```yaml
services:
  worker:
    logging:
      driver: journald
      options:
        tag: botman-my-stack-worker
```

Botman derives and stores the expected identifier. The uploaded Compose file is validated against managed apps before being accepted/deployed.

Docker documents that journald `tag` populates `CONTAINER_TAG` and `SYSLOG_IDENTIFIER`. Podman supports journald `tag`; integration tests must verify `SYSLOG_IDENTIFIER` on every supported runtime/version before production use.

No normal log path should require `docker ps`, `podman ps`, generated container names, or Compose labels.

## Live subscription state

Central config is authoritative:

```yaml
live_logs:
  enabled: true
  thread_id: "..."
  subscription_id: "..."
```

Agent state persists per stack-local app. The generated agent config uses an internal `<stack>.<app>` key so repeated names on one VPS cannot collide:

```json
{
  "subscription_id": "...",
  "cursor": "...",
  "last_realtime_usec": 0
}
```

### `/livelogs start APP`

- Botman resolves the current channel to the stack, then resolves `APP` only inside that stack.
- If disabled: create a new public thread, create a new `subscription_id`, enable live logs, rewrite agent config, restart agent.
- If already enabled: treat the command as **repair/idempotent start**, not “already running” failure.
  - fetch/check configured thread;
  - if archived and reusable, attempt to unarchive it;
  - if deleted, locked, or otherwise unusable, create a replacement thread and new subscription ID;
  - rewrite agent config and restart agent so a previously suspended task resumes.

Threads may auto-archive after inactivity. Discord says archived threads can be reopened; locked threads require moderator capability. Do not depend on a webhook magically repairing every archived/locked state. Integration-test the exact webhook behavior and classify fatal destination failures conservatively.

### `/livelogs stop APP`

- set `enabled=false`
- clear active thread ID from agent config
- optionally archive the existing thread from central
- rewrite agent config and restart agent
- next `start` creates a new subscription session and begins from the live tail rather than replaying the stopped period

## Restart/gap policy

The user explicitly prefers `/logs download` over flooding Discord with a long missed backlog.

Recommended v1 behavior:

- same subscription + short agent/config restart: resume from persisted cursor
- if persisted cursor age exceeds a configurable `resume_max_age_sec` (proposed default: 300 seconds), seek tail instead and post a short gap marker through the webhook
- new `subscription_id`: always start at tail

Store the last delivered realtime timestamp with the cursor so this decision does not require guessing.

The exact default (`300s`) is an implementation default, not a product invariant; make it global config.

## No queue / bounded memory rule

Each app live task processes journal entries in order and does **not** read indefinitely ahead of Discord.

```text
read next entry/bounded group
      ↓
render outbound message(s)
      ↓
send in order
      ↓
ACK all pieces required for safe cursor advancement
      ↓
persist cursor
      ↓
read more
```

Transient network/429/5xx errors cause that app task to wait/retry while journald continues retaining the backlog. Other apps have independent tasks and continue.

Clearly fatal destination errors (invalid/deleted webhook/thread, permission failure, locked/unusable destination as determined by tested Discord responses) suspend that app task without consuming more journal entries or hot-looping. Central `/livelogs start` repairs and restarts it.

## Discord batching and truncation

### Requirements

- Preserve global journal order for the app.
- Keep complete normal journal entries together when they fit.
- Normal multi-line content must not be discarded simply because the whole entry does not fit one Discord message.
- Split oversized multi-line entries **between lines** and immediately send the remaining lines in following messages.
- Only truncate when a single logical line itself exceeds the usable Discord content budget.
- A truncated line is sent alone with an explicit marker.
- Do not checkpoint a journal entry until all outbound messages representing that entry have been acknowledged.

### Suggested algorithm

Use a formatter with a final rendered-content budget, not a raw pre-sanitization guess.

For each journal entry:

1. Split its `MESSAGE` into logical lines while preserving their order.
2. If the complete entry fits in the current Discord batch, append it.
3. If it does not fit, flush any preceding complete-entry batch first.
4. Process this entry as a dedicated sequence:
   - pack as many **whole logical lines** as fit into each Discord message;
   - when one line alone exceeds the budget, flush any pending normal lines, render a dedicated truncated form of that line, then continue with following lines;
   - the final outbound message for the entry carries the entry's safe checkpoint cursor.
5. Continue with later journal entries only after this entry's sequence has been sent in order.

A huge line marker should be unambiguous, e.g.:

```text
<visible prefix> … [line truncated for Discord; original length=18492 chars; use /logs download]
```

Historical export retains the original message.

### Markdown safety

If code fences are retained for readability:

- sanitize embedded triple backticks in a reversible/display-safe way
- calculate size **after** sanitization + wrapper overhead
- set webhook `allowed_mentions` to disable mention parsing

The formatter itself should own size calculation; callers must not use ad-hoc `text[:1800]` truncation.

## Checkpoint semantics

At-least-once delivery is sufficient.

- Persist cursor only after the corresponding complete journal entry has been represented successfully in Discord (including every line-split message for that entry).
- If Discord ACKs but the process crashes before state save, a small duplicate on restart is acceptable.
- Never advance past a partially delivered entry.
- State save should use atomic replace and restrictive permissions.

## Historical export CLI

Install a CLI from the same agent package, e.g. `botman-log-export`.

It is short-lived and invoked by central through SSH. The daemon has no RPC/API.

Proposed interface:

```text
botman-log-export \
  --config /var/lib/botman-log-agent/config.yaml \
  --app app-a \
  --since-utc 2026-09-29T07:00:00Z \
  --until-utc 2026-09-29T09:00:00Z \
  --format human|jsonl \
  --output-dir /var/tmp/botman-export-<id> \
  --max-part-bytes <derived-from-discord-limit>
```

The CLI reads only apps defined in agent config; it should not accept an arbitrary journal match expression from Discord input.

### Human format

Default `.log.gz`. Include an ISO timestamp for each journal entry and preserve the complete message, including multiline content.

### JSONL format

`.jsonl.gz`, one JSON object per journal entry, at minimum:

- timestamp UTC
- configured app name / identifier
- message
- cursor (optional but useful for diagnostics)
- selected stable journal metadata only; do not dump arbitrary unbounded fields by default

### Attachment splitting

Central determines the actual Discord guild upload limit when possible and requests parts under a conservative threshold. Split at journal-entry boundaries when possible. If one raw entry itself exceeds a part budget, file-level splitting is permitted because this is an artifact, not Discord live presentation.

Remote temporary export files are always cleaned after successful download and best-effort cleaned on failure.

## Journald retention

Historical download depends on the local journal, so provisioning must verify persistent journal storage.

Target policy until external archival exists:

```ini
[Journal]
Storage=persistent
SystemMaxUse=1G
Compress=yes
```

This is a **global VPS journal cap**, not a 1 GiB per-app guarantee. High overall system/app log volume can rotate old entries sooner. Do not claim a guaranteed number of days.

Provisioning should be careful with an existing administrator-managed journald configuration: use a clearly named drop-in and report what it changes.

## Agent privileges

Prefer a dedicated service account with journal read permission (`systemd-journal` group where supported), not root and not Docker-group membership. This assumes rootful Docker/Podman logs are present in the system journal; integration tests must cover the actual deployment mode.
