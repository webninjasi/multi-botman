# Configuration Model and Command UX

## Fresh config model

Do not preserve the old per-app duplicated `compose_dir`/`compose_file` design. Introduce stacks.

Illustrative schema (field names may evolve during implementation, semantics should not):

```yaml
settings:
  timezone: Europe/Istanbul
  command_prefix: "!"
  journal_max_use: 1G
  live_resume_max_age_sec: 300
  release_keep_count: 3
  git_known_hosts: /etc/botman/git_known_hosts
  repo_cache_root: /var/lib/botman/repos
  deploy_key_root: /var/lib/botman/keys
  log_export_bin: /opt/botman-agent-venv/bin/botman-log-export
  agent_config_path: /var/lib/botman-log-agent/config.yaml

servers:
  local:
    type: local
    compose_argv: [sudo, podman, compose]

  oracle-2:
    type: ssh
    host: 1.2.3.4
    port: 22
    user: botmgr
    key: /home/botman/.ssh/server-oracle-2
    compose_argv: [sudo, podman, compose]

stacks:
  bots:
    server: oracle-2
    channel_id: "123456789012345678"
    project_name: botman-bots
    compose_file: compose.yml
    apps:
      app:
        service: app
        env_file: env/app.env
        log_identifier: botman-bots-app
        git:
          repo_url: git@github.com:owner/app.git
          branch: main
          deploy_key_path: /var/lib/botman/keys/bots/app
        log:
          webhook_id: "..."
          webhook_url: "..."
          live_enabled: false
          thread_id: null
          subscription_id: null

  website:
    server: oracle-2
    channel_id: "223456789012345678"
    project_name: botman-website
    compose_file: compose.yml
    apps:
      app:  # same generic name is valid because app names are stack-local
        service: app
        log_identifier: botman-website-app
        git:
          repo_url: git@github.com:owner/website.git
          branch: main
```

Prefer derived standard app/stack paths. The central helper locations above are configurable absolute paths so nonstandard target installations can be supported without code changes:

- stack root: `/srv/botman/stacks/<stack>`
- app release root: `<stack>/apps/<app>`
- env default: `<stack>/env/<app>.env`

## Validation

At load and before save:

- server type enum only (`local`, `ssh`)
- positive SSH port
- safe stack/app names (restrict to a conservative lowercase identifier subset)
- safe Compose service name
- channel IDs are numeric strings
- stack references existing server
- apps are nested under their owning stack; app names only need to be unique within that stack
- each configured stack has a globally unique command-channel ID
- Compose project name is stable/safe and unique among stacks on the same server
- each managed Compose service is unique within a stack
- timezone resolvable by `zoneinfo`
- numeric settings > 0 and bounded sensibly
- no unknown keys silently accepted

Use atomic replace, restrictive config permissions, and an async mutation lock in the central process.

## Authorization/routing

For every lifecycle/log/update command:

1. resolve the stack from the invocation channel ID
2. resolve the requested app name only inside that stack
3. reject if the channel is not a configured stack channel or the app does not exist in that stack

Never search other stacks for a matching app name. `app`, `db`, `worker`, and similar generic names may be reused in different stacks without ambiguity.

`/config` commands additionally require the invoking user ID in parsed/whitespace-stripped `ADMIN_IDS`. `/env` commands intentionally do not: they use the same configured stack-channel/app authorization boundary as `/update`, while remaining slash-only and ephemeral because they can expose secrets.

The Discord category is organizational. The configured **command channel** is the actual routing/security boundary.

## Lifecycle/user commands

Use discord.py hybrid commands/groups so both slash and prefix workflows remain available where useful.

Proposed behavior:

```text
/start APP
!start APP

/stop APP
!stop APP

/restart APP
!restart APP

/status APP
!status APP

/update APP
!update APP
```

### Logs group

Preferred UX:

```text
/logs tail APP [lines]
/logs download APP from:<local time> to:<local time> format:<human|jsonl>

!logs APP [lines]                         # convenient prefix tail fallback
!logs download APP "YYYY-MM-DD HH:MM" "YYYY-MM-DD HH:MM" [human|jsonl]
```

Implementation can use a hybrid group with an `invoke_without_command`/fallback tail behavior if discord.py supports the desired slash mapping cleanly. If that becomes awkward, preserve the easy prefix `!logs APP` and use explicit slash subcommands; UX matters more than forcing identical parser structure.

Tail logs come from the journald export/helper path, not `compose logs`.

### Live logs

```text
/livelogs start APP
/livelogs stop APP

!livelogs start APP
!livelogs stop APP
```

`start` is idempotent/repairing. It never acts as a stop toggle.

## Admin config commands

Keep admin management slash-only and ephemeral.

Suggested group structure:

```text
/config server add
/config server edit
/config server test

/config stack add
/config stack edit
/config compose upload
/config compose show

/config app add
/config app edit
/config git setup APP
/config git rotate-key APP

/config agent sync
/config agent status
```

Exact Discord nesting can be adjusted to platform limits, but preserve the concepts.

### Edit semantics

`/config server edit NAME` updates only fields explicitly supplied. It may switch a
server between local and SSH modes; switching to SSH requires `host`, `user`, and
`key`. `clear_known_hosts:true` removes an explicit known-hosts path and returns to
normal OpenSSH/AsyncSSH known-hosts discovery; it never disables host verification.
Run `/config server test NAME` after connectivity/runtime edits.

`/config stack edit` resolves the stack from the current channel and therefore does
not expose a stack-name option. Server, Compose project name, and Compose filename
may be corrected only before apps are added. Once a stack contains apps, changing
those identity/location fields would require moving deployed files/containers, so the
edit is rejected rather than leaving partially migrated state. Stack renames are not
supported.

`/config app edit APP` also resolves the stack from the current channel. It can
update the Compose service, Git repository URL/branch, and journal identifier without
renaming the app. A journal identifier cannot be changed while live logging is
enabled; stop live logs first so the target agent cannot retain a stale subscription.
Edits do not upload Compose, restart containers, rotate keys, or deploy automatically.

### Stack creation

Prefer invocation channel as the stack command channel. Require server and stack name. Derive stack filesystem/project path/name where possible rather than accepting arbitrary shell paths.

### App creation

Run `/config app add` in the stack's command channel. The stack is derived from that channel; do not ask for a stack argument. Require:

- app name (unique only within this stack)
- Compose service name
- repo URL
- branch

The same channel-derived stack rule applies to `/config compose upload|show`, `/config git setup APP`, `/config git rotate-key APP`, and `/env ...`. The `/config` forms remain admin-only; `/env` uses stack-channel authorization like `/update`.

Create/get the app-specific Discord log webhook in the stack command channel. Generate the central repo deploy key during explicit Git setup (or app creation if UX is cleaner) and show only the **public** key ephemerally.

No application deployment happens automatically. Stack-scoped admin mutations that can affect runtime behavior share the same per-stack lock as lifecycle/deployment operations. Store-backed lifecycle, deployment, and Compose upload commands reload/re-authorize configuration after they obtain that lock so queued work cannot run with a pre-edit snapshot.

## Environment commands

Slash-only and ephemeral, but **not** `ADMIN_IDS`-gated. Authorization matches `/update`: the invocation channel must be the configured stack channel and the requested app must exist in that stack:

```text
/env show APP
/env upload APP file
/env set APP key value
/env unset APP key
```

No modal raw editor.

Rules:

- distinguish “file missing” from SSH/SFTP/read failure
- never treat a generic read error as empty content
- validate keys (`[A-Za-z_][A-Za-z0-9_]*`)
- preserve comments/order when set/unset
- write mode `0600`
- upload size sanity limit
- env changes never auto-restart/deploy
- env upload/set/unset serialize against lifecycle/deployment work for that stack; set/unset hold the lock across read-modify-write
- user explicitly runs `/restart` or `/update` when ready

## Config/Compose changes

Configuration changes and Compose uploads do **not** automatically deploy/rebuild managed apps.

Logging state changes are the exception: `/livelogs start/stop` may rewrite agent config and restart `botman-log-agent` because that changes only the logging transport, not the app deployment.
