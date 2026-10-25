# 2026-09-30 12:45 — Docker Compose bootstrap correction

- Target setup now treats an existing Docker CLI/engine as the selected runtime even when the Compose v2 plugin is missing.
- On apt/dnf hosts, setup tries `docker-compose-plugin` and `docker-compose-v2` from configured repositories, and only then verifies `docker compose`, failing with an actionable message.
- `botmgr` is created before target runtime/plugin setup, so a partial install failure can be fixed and setup simply rerun without leaving the management identity absent.
- Added regression coverage for Docker-without-Compose detection and setup ordering.
