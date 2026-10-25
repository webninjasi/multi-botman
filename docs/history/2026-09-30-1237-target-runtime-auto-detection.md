# 2026-09-30 12:37 — Target runtime auto-detection

`setup.sh --mode target` is now runtime-aware instead of blindly installing Podman.

- It detects an existing usable `docker compose` or `podman compose` before attempting runtime installation.
- Docker targets add `botmgr` to the Docker socket's owning group, use `docker compose`, and skip all Podman linger/user-socket setup. Podman targets remain rootless with the user-manager/socket setup.
- Added `--runtime auto|docker|podman`; ambiguous hosts with both runtimes require an explicit choice.
- Target prerequisite installation no longer unconditionally requests the `podman` package, avoiding failures on Docker-only distributions where no Podman package exists.
- A target registered through Discord must use the matching `compose_argv` (`docker compose` or `podman compose`).
