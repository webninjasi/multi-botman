# 2026-11-20 14:00 — Docker/Podman runtime integration CI

## Problem

Compose deployment and lifecycle were only unit-tested against fake executors. Nothing exercised real `docker compose` / `podman compose` behaviour, such as recreate-on-new-image, `--env-file` on build, or the journald tag.

## Change

- `tests/integration/test_compose_runtime.py` drives the real `ComposeAdminService.upload`, `DeploymentService.update`, and `LifecycleService` through `LocalExecutor` against a two-app busybox stack under `/srv/botman/stacks/it-<random>`. The only stand-in is Git: `LocalRepoGit` serves commits from local fixture repos. Archive, upload, checksum, release switch, build, and up are real.
- Scenarios:
  - updating app A recreates only A, and its named volume survives
  - a same-SHA update is a no-op
  - a broken build restores `current` and leaves the old container running
  - stop/start/restart/status touch only the requested service
  - `/rebuild` bakes `build.args` from the app env file
  - `/apply` picks up an uploaded Compose edit
  - container output reaches journald under the configured `SYSLOG_IDENTIFIER`
- Containers are found by Compose labels and inspected with the runtime CLI, so the same assertions run under any Compose provider.
- The tests are skipped unless `BOTMAN_IT_COMPOSE` is set. They carry the `integration` marker, which is registered in `pyproject.toml`.
- `.github/workflows/runtime-integration.yml` runs them on ubuntu-24.04 in three jobs: Docker Compose, Podman with docker-compose over the rootless socket (the `scripts/setup.sh` layout), and Podman with podman-compose. To save runner time, it triggers only on pushes that touch the runtime path or the tests, or by hand. Each job takes about a minute.

## Product fixes found by the workflow

- **podman-compose kept the old container after a build.** Its `up -d` recreates only on a Compose config change, not on a new image, so `/update` and `/rebuild` kept running the previous build. All post-build `up` calls (the `/update` activation, the rollback, and `/rebuild`) now use `ComposeManager.recreate_argv`, which adds `--force-recreate`. `/apply` and the plain `up` are unchanged.
- **podman-compose `ps` rejects service arguments.** `/status` now falls back to the whole-stack `ps` and notes this in the reply. The stack channel already authorizes every service in the stack.
- **Docker could build stale files (intermittent).** `git archive` stamps files with the commit time, and `tar -x` kept those mtimes. BuildKit's incremental context transfer skips same-size, same-mtime files at the same context path (`apps/<app>/current`), so two quick commits could deploy the old content. Releases are now extracted with `tar --touch`. Layer caching is content-based, so nothing is lost.

## Not covered

SSH transport, the real Git fetch, cysystemd reading, and `scripts/setup.sh` provisioning. The CI runner is not a production target.
