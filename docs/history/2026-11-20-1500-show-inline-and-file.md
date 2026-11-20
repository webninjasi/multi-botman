# 2026-11-20 15:00 — `/env show` and `/config compose show` reply inline and attach the file

## Change

- New `src/botman/commands/replies.py` with `send_file_preview`. It always attaches the file. If the header plus a fenced code block fits in Discord's 2000-character message limit, and the content has no ```` ``` ````, the code block goes in the message too. Otherwise the message is just the header.
- `/env show APP` used to send only the attachment. It now also shows the content inline (`ini` block), using filename `APP.env`.
- `/config compose show` used to send *either* an inline block (up to 1600 characters) *or* the attachment. It now always attaches `STACK-compose.yml` and shows it inline (`yaml` block) whenever it fits.
- Both replies stay ephemeral.

## Tests

- `tests/central/test_command_replies.py` covers inline, at the limit, too long, and code fence fallback.
- Ruff and mypy pass. On Windows, `test_admin_discord_registration.py::test_agent_sync_uses_shared_server_lock` fails both with and without this change (the known Windows-only failures).
