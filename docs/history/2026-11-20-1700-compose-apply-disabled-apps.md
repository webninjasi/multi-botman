# 2026-11-20 17:00 — `/config compose apply` and disabled apps

## Why

Uploading a Compose file changed nothing until something ran `up`, and Botman could only `up` managed apps one at a time. A service that was not a managed app (db, redis) could not be applied, and a service dropped from the file kept running with no way to remove it. Upload also refused any file that left out a configured app's service, so an app's service could not be taken out.

## Change

- **The Compose file is the source of truth.** `validate_compose_yaml` no longer requires every configured app's service. A configured app whose service is absent is disabled. Services that are present still get the journald/tag/build-context checks. New `parse_compose_yaml` holds the shared safe-parse rules.
- **Disabled apps.** `require_enabled` reads the installed file (`cat`, with `test -e` only on failure). `LifecycleService` and `DeploymentService.update` call it under the stack lock before any Compose or Git work. `/update` checks it before its first progress note, so a disabled app gets a plain reply and no thread. The lifecycle and update adapters show `AppDisabledError` as is. Nothing about the app is deleted.
- **`/config compose apply`** (`ComposeAdminService.plan_apply` / `apply`, Discord glue in `commands/compose_apply.py`):
  - the plan runs `up` for every service except undeployed managed apps (no `apps/APP/current`, which would make `up` build from a missing context), services with `profiles`, and services that `depends_on` a skipped one;
  - orphans are containers with the stack's `com.docker.compose.project` label whose `com.docker.compose.service` is not in the file, listed with the container runtime (`container_runtime_argv` derives `docker`/`podman` from `compose_argv`, keeping a `sudo` prefix);
  - an ephemeral preview has Apply/Cancel buttons. Apply recomputes the plan under the stack lock and refuses if it changed (`ComposeApplyStaleError`), runs `rm -f` on orphan containers, then brings services up;
  - CI showed that docker-compose on Podman recreates every container on a plain `up`, even with no changes. So when `compose config --hash '*'` works, the plan compares each service's hash with its containers' `com.docker.compose.config-hash` label: unchanged services get `up -d --no-recreate`, then new or changed ones get `up -d --no-deps`. Without hash support (podman-compose) it runs one plain `up -d`;
  - volumes and networks are never deleted.
- The `/config compose upload` reply now has an **Apply now** button that opens the same preview.

## Tests

- Unit: validation with a missing service, disabled app rejection (plain and store-backed lifecycle, `/update` before Git/progress, both adapters), a missing Compose file, runtime derivation, plan skips (undeployed, dependents, profiles), orphan detection, apply command order, the hash-based unchanged/changed split and its plain-`up` fallback, the stale-plan refusal, and plan rendering.
- Integration (`tests/integration/test_compose_runtime.py`, all three CI providers): apply adds a new non-app service and leaves unchanged containers alone (also on a repeated no-change apply), then removes it after it is dropped from the file; an app dropped from the file is removed by apply, refused as disabled, keeps its release, and comes back after re-adding and applying.
