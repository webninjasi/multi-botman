# Deployment Design

## Principle

The **central bot** owns Git and deployment orchestration. The logging daemon is not involved.

Target VPSes receive immutable-ish source release archives and only need Compose/container tooling, Python for the logging package, and SSH/SFTP management access.

## Central Git repositories

Each app has:

- `repo_url`
- `branch`
- central `deploy_key_path`

Recommended cache layout:

```text
/var/lib/botman/repos/<stack>/<app>.git
```

Use a bare mirror/cache and fetch the configured remote branch on `/update`.

### Git SSH authentication

Use one read-only SSH deploy key per stack-local app/repo on the central VPS. Cache and key paths are namespaced by stack so generic app names can repeat across stacks.

This removes Git secrets from managed VPSes, but the central machine still must verify the Git provider's SSH host key. Use normal OpenSSH strict host checking and a separately managed `known_hosts` file (for example `/etc/botman/git_known_hosts`). Do not:

- set `StrictHostKeyChecking=no`
- use `known_hosts=None`
- dynamically trust `ssh-keyscan` output without administrator verification
- hard-code provider host keys inside Python source

Git commands should be launched with argv-safe subprocess APIs and a controlled environment; avoid building shell command strings from config.

## Release archive

After `git fetch`:

1. resolve configured branch to an exact commit SHA
2. query target active SHA
3. if identical, report no-op and stop
4. generate a source archive from that exact commit (`git archive` is preferred for ordinary repos)
5. generate SHA-256 of archive
6. SFTP to a unique remote temporary path

V1 assumptions:

- normal Git-tracked files
- no Git submodule/LFS special handling unless specifically added later
- release does not contain `.git`

Submodules/LFS belong in `ROADMAP.md` unless real apps require them.

## Stack/release layout

```text
/srv/botman/stacks/<stack>/
├── compose.yml
├── env/<app>.env
└── apps/<app>/
    ├── current -> releases/<sha>
    └── releases/<sha>/...
```

Compose for a managed app should use the stable path:

```yaml
services:
  app-a:
    build:
      context: ./apps/app-a/current
```

The project name is always supplied explicitly (e.g. `-p botman-<stack>`) so changing release directories never creates a new Compose project.

## Staged activation algorithm

Use a per-stack operation lock.

1. Create deployment thread under stack command channel.
2. Fetch repo and resolve SHA.
3. Read target `current` symlink / release metadata.
4. If already on SHA: finish as no-op.
5. Upload archive + expected checksum.
6. Target verifies checksum.
7. Extract into `releases/.<sha>.tmp` using safe flags; reject unexpected extraction failures.
8. Write release metadata inside stage (app, repo, branch, SHA, deployment timestamp, archive hash).
9. Rename temp release directory to `releases/<sha>`.
10. Record previous `current` target.
11. Atomically switch `current` symlink to the new release.
12. Run Compose build for **only** the target service. Existing container continues running its old image during build.
13. If build fails:
    - restore `current` symlink to previous release;
    - report failure;
    - leave running container untouched;
    - keep transcript.
14. If build succeeds, run Compose up for the target service only (`up -d --no-deps <service>` unless runtime compatibility requires an equivalent).
15. If activation fails:
    - restore previous `current`;
    - attempt a best-effort rollback activation/build as required;
    - report both the original error and rollback result loudly.
16. On success, retain a small configurable number of recent releases (proposed default 3) and delete older inactive release directories.
17. Delete remote temporary archive.
18. Attach full deployment transcript to Discord thread.

### Why switch `current` before build?

The shared Compose file points at a stable `current` path. Switching the symlink does not modify an already-running container. A failed build can therefore restore the old symlink without having touched the live container.

This is not a perfect transactional orchestrator, especially if `compose up` fails during replacement, so explicit rollback logging/tests are mandatory.

## Multi-app shared Compose implications

A stack's Compose file may contain multiple app services and supporting services.

- `/update app` first resolves the current channel to a stack, then builds/ups only that stack-local app service.
- Do not run a blanket `compose up` that recreates unrelated services.
- Default to `--no-deps` for app-specific update/start when appropriate so another service is not silently restarted.
- Shared dependencies are managed deliberately through Compose configuration, not by auto-deploying every service whenever one repo changes.
- Concurrent Compose mutations inside the same stack are serialized.

## Compose configuration upload

Compose is bot-managed and shared by the stack, so retain an admin upload command.

Upload flow:

1. receive attachment ephemerally/admin-only
2. parse YAML safely
3. store as standard target filename (proposed `compose.yml`)
4. validate all currently configured managed app service names exist
5. validate managed app journald logging convention (`driver: journald`, expected tag)
6. validate managed app build context follows the standard release layout where the service is Git-deployed
7. run runtime `compose config` against the target after upload where feasible
8. never auto-rebuild/restart apps merely because Compose config changed

When a new app is added after the Compose file, validate that app's service against the existing Compose file.

## Deployment output

`/update` and `!update` create a thread under the stack command channel.

Post concise line-oriented progress there:

- repo fetch
- resolved commit
- no-op/current commit
- archive/upload/checksum
- staging
- Compose build output
- activation output
- rollback output when applicable
- final status

Maintain a complete plain-text transcript in parallel. At the end, attach it; if larger than the Discord upload limit, gzip/split it using the shared attachment utility.

Do not route Compose build/up output through the journald live-log daemon. Runtime logs and management/deployment output are distinct streams.

## Lifecycle command output

- `/start`, `/stop`, `/restart`, `/status`: output goes directly to the stack command channel, not a deployment thread.
- Capture stdout/stderr and use the shared Discord text formatter for concise output.
- If unexpectedly long, attach the full command transcript rather than silently throwing it away.

## Future auto-deploy

Not in v1.

Planned pattern:

- GitHub Actions posts via a known per-app Discord webhook.
- Body contains non-secret fields such as commit/ref/timestamp/nonce and a hex/base64 HMAC signature.
- Bot maps `message.webhook_id` to app.
- Bot verifies HMAC over a canonical payload with `secrets.compare_digest`.
- Enforce timestamp/replay window and optionally nonce cache.
- Valid trigger calls the exact same deployment method as `/update`.

The HMAC secret is never included in Discord message content.
