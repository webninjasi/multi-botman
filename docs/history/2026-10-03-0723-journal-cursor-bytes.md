# 2026-10-03 07:23 — Encode journal cursors for cysystemd

- Fixed live-log resume: persisted cursors are text, but cysystemd 2.x `seek_cursor()` requires bytes; cursors are now UTF-8 encoded at the adapter boundary.
- Tightened the journal reader test double to reject string cursors and added an assertion that resume passes bytes, reproducing the production TypeError as a regression test.
