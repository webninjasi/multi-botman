# 2026-09-30 04:43 — Stack-local app namespace correction

- Moved managed apps under each stack in the central YAML model; app names are no longer globally unique.
- Runtime commands now resolve Discord channel -> stack first, then resolve the app only within that stack.
- `/config app add`, Compose upload/show, Git setup/rotation, and `/env` infer the stack from the command channel instead of asking for a stack argument.
- Namespaced central Git caches and deploy keys by stack (`repos/<stack>/<app>.git`, `keys/<stack>/<app>`).
- Namespaced target log-agent/export identities as `<stack>.<app>` so duplicate app names on one VPS do not share checkpoints or exporter entries.
- Added one-way loading support for the earlier flat top-level `apps:` schema; the next config save writes the corrected nested representation.
- Added regression coverage for duplicate `app`/`db` names across stacks, Discord command registration, Git/key paths, and target agent config.
- Automated suite reached 118 passing tests.
