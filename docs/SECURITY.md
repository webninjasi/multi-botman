# Security and Trust Boundaries

## Primary boundaries

1. Discord channel/category permissions decide who can reach an app's command channel.
2. Botman resolves command channel -> stack first, then resolves the app only inside that stack for every lifecycle/log/deployment command.
3. `ADMIN_IDS` is an additional explicit gate for `/config` management; `/env` intentionally follows the same stack-channel/app authorization boundary as `/update`.
4. Central SSH access is the management transport; no new network management ports are exposed.
5. The log agent holds only what it needs for logging (journal mapping + active Discord webhook credentials), never Git credentials.

## Central service account

Run the central bot as a dedicated unprivileged Linux user, not root.

- config and environment file owned by that user, `0600`
- central -> VPS SSH keys owned by that user, `0600`
- per-app Git deploy private keys owned by that user, `0600`
- narrowly scoped sudo only where local Compose/provisioning requires it

Do not put `DISCORD_TOKEN` directly in a world-readable unit file. Use a protected `EnvironmentFile=` or service credential mechanism.

## SSH to managed VPSes

Current archived code disables host verification (`known_hosts=None`); this must not be copied.

Fresh executor requirements:

- strict host-key verification enabled
- use the central service user's known_hosts or an explicit managed known-hosts file
- unknown/mismatched host key is a hard failure
- connection establishment itself must be covered by timeout
- no password auth fallback unless explicitly added later
- remote account should be a dedicated management user with narrow sudo permissions

Do not automatically trust `ssh-keyscan` output in code.

## Git provider SSH

Each stack-local app gets a separate central read-only deploy key; key/cache paths are namespaced by stack so repeated app names cannot collide.

Server authenticity is separately verified using OpenSSH `known_hosts` on the central VPS. Deploy keys answer “is this client allowed to read the repo?”; host keys answer “is this really the Git server?”. Both are needed for Git-over-SSH.

Do not embed GitHub's current host key in Python source. Administrator provisioning updates the trusted known-hosts file based on the provider's published fingerprints.

## Command execution

Fresh executor API should distinguish argv execution from shell scripts.

- local: `asyncio.create_subprocess_exec(*argv)` by default
- SSH: build a safely quoted remote command from an argv sequence (`shlex.join`) in one controlled helper because SSH ultimately invokes a remote shell
- do not interpolate app/service/branch/path text into hand-built shell strings
- prefer derived/validated paths
- Compose command should be represented as an argv list, not an arbitrary shell snippet
- no arbitrary “run shell” Discord command

## SFTP/file operations

- unique temporary filenames (random/UUID), never a single PID-derived temp shared by concurrent writes
- verify command result after privileged move/chmod
- use atomic replacement where possible
- sensitive files (`config`, `.env`, agent config, keys) restrictive permissions
- uploaded Compose/config files have size limits and safe YAML parsing

## Discord webhook credentials

Incoming webhook URLs are bearer credentials. The log agent necessarily needs the active app's log webhook URL.

- agent config must be protected
- consider omitting webhook URL from agent config for disabled live streams
- never print webhook URLs in public responses/logs
- one app webhook is retained for isolation/rate-limit independence unless implementation profiling gives a reason to consolidate

Webhook payloads should disable mention parsing.

## Secret-bearing Discord interactions

`.env` contents, public deploy keys, and necessary setup secrets are displayed only in **ephemeral slash-command interactions**. Never expose these in normal channel messages or prefix-command replies.

`/config ...` remains `ADMIN_IDS`-gated. `/env ...` is deliberately not admin-only: anyone permitted to operate apps from a configured stack command channel (the same trust boundary used by `/update`) may show or modify that stack's app environment. Therefore Discord access to a stack command channel is also access to that stack's application secrets. Keep stack-channel membership/permissions accordingly restrictive.

Because prefix commands cannot be ephemeral, `/env` remains slash-only even though `/update` also has a prefix form.

## Deployment source archive

- generated centrally from a known Git commit
- checksum before transfer; verify on target
- extract only into a unique staging directory
- never extract directly over active source
- do not preserve archive owner/setuid metadata
- verify/limit archive paths; `git archive` from a trusted fetched repository is preferred over arbitrary user-uploaded tarballs

## Config concurrency

Central configuration writes should be serialized with an `asyncio.Lock`. Atomic file replacement prevents partial files but does not by itself prevent two interactions from overwriting each other's in-memory changes.
