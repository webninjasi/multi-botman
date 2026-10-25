# 2026-09-30 11:43 — Env authorization aligned with deployers

- Moved `/env show|upload|set|unset` out of the administrator command registration path.
- `/env` now uses the same stack-channel/app authorization boundary as `/update` instead of requiring `ADMIN_IDS`; commands remain slash-only and ephemeral because they may expose secrets.
- Added Discord registration/authorization regression coverage; automated suite is now 170 tests.
