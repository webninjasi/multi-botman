# 2026-09-30 10:38 — Git deploy-key argv validation fix

`/config git setup` previously failed before launching `ssh-keygen` because `LocalGitRunner` rejected the intentional empty passphrase argument in `ssh-keygen -N ""`.

- `LocalGitRunner` now allows legitimate empty non-executable subprocess arguments.
- Empty executable names and NUL-containing arguments remain rejected.
- Added regression coverage for both behaviors.
- Added `reference/` to `.gitignore`.
