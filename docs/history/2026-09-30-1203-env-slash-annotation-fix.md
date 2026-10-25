# 2026-09-30 12:03 — `/env` slash-command annotation fix

- The stack-authorized `/env` module no longer uses postponed annotations for nested slash callbacks. Previously discord.py raised `NameError: name 'discord' is not defined` while resolving `discord.Interaction` / `discord.Attachment` during bot startup.
- `/env` now exposes concrete runtime Discord annotation types when commands are registered.
- Added regression coverage asserting those annotations resolve to concrete runtime types.
