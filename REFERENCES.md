# External Technical References

Checked during the 2026-09-30 planning pass. Re-check current upstream docs before implementing behavior that may have changed.

## cysystemd

Project: `https://github.com/mosquito/cysystemd`

PyPI: `https://pypi.org/project/cysystemd/`

Planning facts checked:

- current PyPI release observed: 2.0.5 (2026-03-04)
- v2 `AsyncJournalReader` uses direct async journal-event iteration rather than the older internal queue/thread design
- `JournalReader`/async reader expose filters, cursor seek, tail seek, realtime seek, entry cursor, and realtime timestamps
- use `JournalOpenMode.SYSTEM` for the rootful system journal use case

## systemd journal field thresholds

`sd_journal_set_data_threshold()` documentation:

`https://www.freedesktop.org/software/systemd/man/latest/sd_journal_get_data.html`

Relevant fact: the default library data threshold may truncate large returned fields; setting threshold to `0` requests complete data fields. The historical exporter therefore must explicitly request complete data. Live reader behavior should be tested before choosing a nonzero threshold.

## Docker journald logging

`https://docs.docker.com/engine/logging/drivers/journald/`

Relevant facts checked:

- journald driver stores `CONTAINER_ID`, `CONTAINER_ID_FULL`, `CONTAINER_NAME`, `CONTAINER_TAG`, `SYSLOG_IDENTIFIER`, `CONTAINER_PARTIAL_MESSAGE`, and `IMAGE_NAME`
- the `tag` log option sets `CONTAINER_TAG` and `SYSLOG_IDENTIFIER`

## Podman journald logging

Podman documentation includes journald as a supported/default driver on typical Linux setups and supports `--log-opt tag=...` for journald.

Example current docs:

`https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html`

The fresh implementation must still run a real Podman integration test proving the configured tag appears as the expected `SYSLOG_IDENTIFIER`; do not rely on documentation interpretation alone.

## Discord threads

Discord thread FAQ:

`https://support.discord.com/hc/en-us/articles/4403205878423-Threads-FAQ`

Relevant behavior checked:

- threads auto-close/archive after configured inactivity
- archived threads can be reopened
- moderator-locked threads require moderator permissions to reopen

Do not hard-code assumptions about the exact webhook HTTP response for archived/locked/deleted threads. Establish those response classes with the Discord integration tests and suspend the agent conservatively on fatal destinations.

## AsyncSSH

Current 2.x API documentation checked during implementation:

`https://asyncssh.readthedocs.io/en/latest/api.html`

Relevant facts checked:

- omitting `known_hosts` uses normal OpenSSH known-hosts lookup; explicitly setting `known_hosts=None` disables server host-key validation
- `connect_timeout` covers TCP establishment plus SSH handshake/authentication
- public-key/password/keyboard-interactive/GSS/host-based authentication can be explicitly enabled/disabled
- `SFTPClient.posix_rename()` replaces an existing destination with POSIX rename semantics and is preferable to default `rename()` for atomic config replacement

