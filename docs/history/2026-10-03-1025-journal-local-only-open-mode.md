# 2026-10-03 10:25 — Journal readers open with `LOCAL_ONLY`

- The live reader (`src/botman_agent/journal.py`) and historical exporter (`src/botman_agent/export.py`) now open the journal with `JournalOpenMode.LOCAL_ONLY` instead of `JournalOpenMode.SYSTEM`.
- Real-host acceptance must confirm this mode sees rootful/rootless container logs on each supported runtime.
- The journal/export test doubles were only updated for `LOCAL_ONLY` on 2026-10-26 (see `2026-10-26-0135-static-gate-first-run-fixes.md`); until then `tests/agent/test_journal.py` and parts of `tests/agent/test_export.py` failed with `AttributeError: ... no attribute 'LOCAL_ONLY'`.
