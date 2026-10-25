# 2026-10-03 10:25 — Journal readers open with `LOCAL_ONLY`

- The live reader (`src/botman_agent/journal.py`) and historical exporter (`src/botman_agent/export.py`) now open the journal with `JournalOpenMode.LOCAL_ONLY` instead of `JournalOpenMode.SYSTEM`.
- `docs/LOGGING.md` still describes `JournalOpenMode.SYSTEM` and should be reconciled once real-host acceptance confirms which mode sees rootful/rootless container logs.
- Follow-up: the journal/export test doubles still only expose `SYSTEM`, so `tests/agent/test_journal.py` and parts of `tests/agent/test_export.py` fail with `AttributeError: ... no attribute 'LOCAL_ONLY'` until the fakes are updated.
