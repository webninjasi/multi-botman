# 2026-09-30 11:33 — Git-layout installer/updater and configurable log exporter

Production installation is standardized on `/opt/botman` as the Git checkout, `/opt/botman-venv` for the central process, and `/opt/botman-agent-venv` for the target agent/exporter.

- Shipped systemd units use those venv paths.
- Added `scripts/setup.sh` for repeatable central/target/all installation, including rootless `botmgr` Podman user-manager/socket setup, journald, agent permissions, venvs, units, and restricted sudoers.
- Added `scripts/update.sh` for clean fast-forward Git updates, venv reinstalls, unit refresh, daemon reload, and configured-service restarts.
- Added `settings.log_export_bin` and `settings.agent_config_path`; historical logs and agent sync now use these configured paths rather than independent hardcoded constants.
