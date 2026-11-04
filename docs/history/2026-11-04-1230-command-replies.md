# 2026-11-04 12:30 — Slash commands always reply; `/update` threads off its reply

## Problem

Some slash commands ended with "The application did not respond".

- `/update` deferred the interaction, then posted only to a new channel thread. It never answered the interaction itself.
- Any exception an adapter did not catch (for example an SSH connection error outside `CommandError`) left the deferred interaction unanswered. The bot had no command error handler.

## Change

- `/update` now replies "Deploying `APP`; progress follows in the thread." and creates the deploy thread from that reply (`Message.create_thread`). The final status (complete, no-op, or failed) is posted in the thread with the transcript, and the reply is edited to match.
  - The reply and thread are still created lazily, so wrong-channel or unknown-app requests get a plain reply and no thread.
  - If the reply cannot be threaded, the bot falls back to `channel.create_thread`. If no thread can be created at all, failures are reported to the command reply.
- New `src/botman/commands/errors.py`, wired in `bot.py` as an `on_command_error` listener and as `tree.error`:
  - logs the full exception;
  - replies with usage/check errors as-is;
  - for anything else, replies with only the exception type name, never its text (it may contain hosts, paths, or secrets);
  - flags the interaction so a hybrid error is not reported twice.
  - Registering the listener replaces discord.py's default traceback print; the handler logs the error instead.

## Tests

- `test_update_command.py`: the reply plus a thread off that reply, the channel-thread fallback, and the starter reply edited on success and failure.
- `test_command_errors.py`: type-only message, followup vs initial response, and a single report across both handlers.
- Not yet verified against real Discord: whether `create_thread` works on interaction followup messages. The channel-thread fallback covers it if not.
