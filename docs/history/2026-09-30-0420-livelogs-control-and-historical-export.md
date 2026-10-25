# 2026-09-30 04:20 — Live-log control and historical export

- Added persistent central `/livelogs start|stop` control with webhook/thread repair, server-scoped synchronization, and rollback if the target agent cannot be restarted.
- Added `/config agent sync|status`; target OS/package provisioning remains an explicit one-time manual runbook step.
- Added `botman-log-export` using cysystemd finite journal reads, protected app-to-identifier lookup, complete human/JSONL gzip exports, size-bounded parts, and empty-range artifacts.
- Added hybrid `/logs tail|download` with app-channel authorization, IANA-timezone conversion, DST gap/ambiguity rejection, target-temp cleanup, and Discord upload budgeting.
- Hardened agent service control to non-interactive `sudo -n` and serialized live-log mutation+sync per target server.
- Automated suite reached 113 passing tests; real VPS/container/journald/Discord acceptance remains outstanding.
