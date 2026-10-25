# Deferred / Future Work

These items are intentionally **not** required for v1 unless a real blocker appears.

## Push-triggered auto-deployment

Keep the useful “no inbound management port” idea, but redesign authentication.

Future design:

- per-app Discord incoming deployment webhook
- GitHub Actions sends commit/ref/timestamp/nonce/signature
- signature = HMAC(secret, canonical payload)
- secret stays in GitHub secret storage + central Botman config only; it is never sent in Discord content
- Botman validates `message.webhook_id`, HMAC via constant-time comparison, timestamp/replay window, then calls the same deployment implementation as `/update`

## External archival sink

Add S3-compatible/object storage or another desired archival target later.

Until then, persistent journald capped at 1 GiB + `/logs download` is the only retained-history mechanism. Do not call it permanent archival.

Potential future archive implementation should have its own cursor/state and must not interfere with Discord live subscriptions.

## Hot agent config reload

Not needed for v1. Agent restart on logging config changes is acceptable.

## Git submodules / Git LFS

Fresh v1 archive path assumes normal Git tracked files. Add explicit support only if an actual app requires it.

## Advanced deployment health gates

Possible later additions:

- Compose healthcheck wait before declaring deployment successful
- app-specific post-deploy smoke command
- explicit `/rollback APP [commit]`
- deployment history command

V1 should retain prior releases and perform best-effort automatic rollback on activation failure, but avoid building a full release-management platform.

## Rich archival/query UI

Possible later:

- `/logs download` presets (“last hour”, “today”)
- external indexed search
- cross-app aggregation

Do not introduce Loki/OpenSearch/etc. unless the project's scale genuinely changes.

## Agent self-update without full provision

Provisioning can initially install/upgrade the agent package and restart it. A versioned incremental updater may be added later only if necessary.

## More runtimes/platforms

Current design assumes Linux + systemd + Docker/Podman Compose-compatible workflows. Non-systemd hosts are out of scope unless requested later.
