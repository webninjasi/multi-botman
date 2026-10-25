# 2026-10-26 01:23 — Live-log batching

The live agent previously sent one Discord message per journal entry. It now batches consecutive entries into one message, as `docs/LOGGING.md` already called for.

- `LiveBatch` (`src/botman_agent/formatting.py`) holds at most one pending message of text. A complete entry is appended when it fits; otherwise the pending batch is emitted first. An oversized multi-line entry still splits between lines, and its final segment starts the next batch.
- `_deliver_batched` (`src/botman_agent/live.py`) sends the pending batch when the next entry would not fit under `discord_message_limit`, or after `batch_max_delay_sec` since the batch's first entry, whichever comes first. At most one journal entry is read ahead.
- Central setting `live_batch_max_delay_sec` (default `2`, `0` = send per entry) is written into the agent config as `batch_max_delay_sec`.
- A cursor is checkpointed only after the message containing that entry's final segment is acknowledged; entries in an unsent batch are re-delivered after a crash, never skipped.
- The agent config uses `extra="forbid"`, so central and agents must be updated together.
- Added `LiveBatch` unit tests and live-loop tests for packing, size flush, delay flush, failed-send checkpoint retention, and zero delay.
