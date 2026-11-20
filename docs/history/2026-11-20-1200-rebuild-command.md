# 2026-11-20 12:00 — `/rebuild APP` lifecycle command

## Problem

Some apps read env/Compose values at build time (for example `build.args` baked into a frontend bundle). No command applied a change to those values:

- `/apply` runs `up -d`, which reuses the existing image, so build-time values stay stale.
- `/update` is a no-op when `current` already points at the fetched SHA, so it never reaches `build`.

## Change

- New `rebuild` lifecycle operation, hybrid `/rebuild APP` / `!rebuild APP`. Under the stack lock it runs `compose build <service>` and then `compose up -d --no-deps <service>` against the active release. It uses the same channel authorization and config reload as the other lifecycle commands (`src/botman/compose.py`, `src/botman/commands/lifecycle.py`).
- If the build fails, `up` does not run, so the running container is left as it was. Output from both steps is merged into one reply, labelled `[build]` and `[up]`.
- The build timeout is 660s, shorter than the 900s `/update` build, so build + up (180s) fits inside Discord's 15-minute deferred-reply window.
- In effect it is `up -d --build --no-deps <service>`, split in two so only the build gets `--env-file`.
- Optional `build_only` (`/rebuild APP build_only:true`, `!rebuild APP yes`) runs only the build, as internal operation `build`, with an 840s timeout. The container keeps its old image until it is recreated.
- It does not fetch or switch releases. New code still goes through `/update`.
- `/env upload|set|unset` and `/config compose upload` replies now mention `/rebuild`.

## Build-time env

Compose `env_file:` only reaches the running container, so `build.args` like `${VITE_API_URL}` resolved empty even when the app env file set them. All Botman builds (`/rebuild`, the `/update` build, and the `/update` rollback build) now go through `ComposeManager.build_argv`. It probes the app env file with `test -f` and, when the file exists, adds `--env-file <app env file>`. Compose rejects a missing `--env-file`, hence the probe.

- `--env-file` goes on `build` only, not `up`, so `/apply`/`/start` behaviour is unchanged. Docker Compose leaves `build` out of its container config hash, so a different build-arg interpolation at `up` time does not recreate the container. This has not been checked on Podman.
- During that build, `--env-file` replaces the stack-root `.env`, and other apps' `${VAR}`s resolve empty, with a Compose warning.

## Tests

- `test_compose.py`: store-backed `rebuild` runs build then up, a failed build stops before `up`, build argv includes `--env-file` when the env file exists, and it is omitted when the file is missing.
- `test_deployment.py`: the `/update` build carries `--env-file` and `up` does not.
- `test_discord_lifecycle.py`: `rebuild` is registered, and `build_only` maps to the `build` operation. `test_compose.py` also checks that `build` never runs `up`.
- Not run on Linux yet. On Windows the new non-store tests pass. The store-backed rebuild test and the deployment tests fail on the known POSIX-path and timezone issues, the same set that failed before this change. Ruff and mypy pass.
