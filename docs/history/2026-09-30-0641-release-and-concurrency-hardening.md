# 2026-09-30 06:41 — Release and concurrency hardening

- Made tests runnable from a clean source checkout without relying on an editable install, added Python 3.11-3.13 CI for tests/compile/wheel build, and added a wheel-content/entry-point verifier.
- Hardened deployment archive validation against escaping symlinks, hard links, and malformed active `current` targets.
- Made Git deploy-key rotation stage and validate the replacement before swapping out the existing keypair.
- Enforced Compose project-name uniqueness per server and managed-service uniqueness within each stack.
- Hardened live-log recovery for unusable cursors, corrupt checkpoints, and unexpectedly ended reader streams.
- Serialized server/stack/app config edits, Git-key setup, `.env` mutations, and manual agent sync against the corresponding runtime operation locks.
- `.env set`/`unset` now hold the stack lock across the complete read-modify-write transaction.
- Store-backed lifecycle, deployment, and Compose upload paths reload/re-authorize config after acquiring the stack lock, preventing queued commands from acting on a stale service/branch snapshot after an admin edit.
- Automated suite is now 155 tests. Real container/systemd/journald/Discord acceptance remains outstanding.
