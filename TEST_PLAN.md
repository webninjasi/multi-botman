# Test Plan

The archived tests are not the acceptance suite for the fresh implementation.

## Unit — config/security

- safe identifiers accepted; traversal/metacharacters rejected where identifiers are required
- unknown YAML keys rejected
- invalid server/stack structure rejected
- timezone validation
- `ADMIN_IDS` whitespace stripped
- config atomic save + permissions
- config mutation lock prevents last-writer races in concurrent interactions
- command channel resolves exactly one stack; app lookup is restricted to that stack
- duplicate app names across different stacks are accepted and resolve independently

## Unit — executor

- local argv execution does not invoke shell interpolation
- timeout kills child process
- SSH host verification enabled
- SSH connect included in timeout
- remote argv quoting correctness
- SFTP unique temp file handling
- privileged write failure propagates
- file mode applied

## Unit — Discord text formatter

Cover exact rendered size including Markdown sanitization/wrappers.

- multiple normal journal entries pack in order
- when next entry cannot fit, current batch flushes at entry boundary
- multi-line oversized entry splits at newline boundaries into consecutive messages
- remaining lines are not discarded
- one huge single line is sent alone and truncated with marker
- huge line followed by normal lines keeps order
- embedded triple backticks cannot break formatting or exceed limit after sanitization
- mentions disabled
- cursor for split entry is commit-eligible only after final segment

Property/fuzz tests should assert:

- every outbound content payload <= platform limit
- original ordering maintained
- every non-huge logical line appears exactly once
- truncation occurs only on a line whose rendered form cannot fit by itself

## Unit — agent state/checkpoints

- atomic state writes
- short same-subscription restart resumes cursor
- new subscription starts at tail
- stale checkpoint beyond resume window starts at tail
- crash after Discord ACK before state write produces duplicate, not loss

## Async — live agent

- transient webhook 500/network/429 blocks that app without reading unlimited journal entries
- other app tasks continue
- fatal destination error suspends task and avoids HTTP hot loop
- cancellation/shutdown exits in bounded time
- unexpected app task error is supervised/restarted
- no Python queue grows with backlog

Use a fake journal adapter for deterministic unit tests; reserve real cysystemd for Linux integration tests.

## Unit — historical exporter

- inclusive start / end semantics are defined and tested
- human output preserves original multiline message
- JSONL parses and preserves message/timestamp
- complete huge message preserved
- empty range yields valid empty export/summary
- split parts respect requested max bytes conservatively
- output filenames safe

## Unit — deployment

- branch resolution exact SHA
- already-deployed SHA no-op
- archive generated from exact SHA
- checksum mismatch aborts before active switch
- unique remote temp names
- release path derived safely
- symlink switch/restore
- cleanup never deletes active release
- keep-count pruning
- full transcript captures stdout/stderr

## Integration — Compose stack

Create a test Compose project with at least two managed services, each from a separate source fixture/repo.

- one Compose file + two apps
- update app A does not restart app B
- start/stop/restart/status target only requested service
- stable project name across release switches
- named volume survives app release update
- build failure leaves old app running/current restored
- successful build/up advances current

Run against supported Docker Compose and Podman Compose environments.

## Integration — journald

On real supported hosts:

- logging driver/tag produces expected `SYSLOG_IDENTIFIER`
- cysystemd filter returns only selected app
- multiline messages
- very long message/line retained by historical reader
- cursor seek/resume behavior
- realtime seek for exports
- agent service user can read required journal without Docker group/root
- persistent journal survives reboot (where practical test environment allows)
- global journal config reports intended 1 GiB cap/drop-in

## Integration — Discord

Use a test guild/channel/webhook.

- webhook posting to live thread
- 429 retry behavior
- archived thread behavior
- locked thread behavior
- deleted thread behavior
- `/livelogs start` repair behavior
- attachment limit discovery + split upload
- deployment thread created beneath command channel

Do not assume exact HTTP error classification until these tests establish it against current Discord behavior.

## Manual failure drills before v1

- reboot target VPS during active live logs
- stop agent for > resume window, generate logs, restart; verify no flood and `/logs download` retrieves gap while retained
- block Discord network temporarily
- delete/lock/archive live thread and exercise repair
- interrupt SFTP release upload
- corrupt uploaded archive
- deliberately fail Dockerfile build
- deliberately make `compose up` fail and inspect rollback result
- rotate app source through several releases and verify cleanup
- run simultaneous commands in same stack to verify locking
- verify an unconfigured Discord channel cannot control an app
- verify the same generic app name in two configured stack channels controls only that channel's stack-local app
