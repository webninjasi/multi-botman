#!/usr/bin/env bash
set -euo pipefail

MODE="auto"
BRANCH="main"
REPO_DIR="/opt/botman"
CENTRAL_VENV="/opt/botman-venv"
AGENT_VENV="/opt/botman-agent-venv"

usage() {
  cat <<'USAGE'
Usage: sudo /opt/botman/scripts/update.sh [options]

Fast-forward the Botman Git checkout, reinstall installed components, refresh
systemd units, and restart services that are already configured.

Options:
  --mode auto|central|target|all   Components to update (default: auto)
  --branch NAME                   Branch to fast-forward (default: main)
  -h, --help                      Show this help
USAGE
}

while (($#)); do
  case "$1" in
    --mode) MODE="${2:?missing value for --mode}"; shift 2 ;;
    --branch) BRANCH="${2:?missing value for --branch}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MODE" in
  auto|central|target|all) ;;
  *) echo "--mode must be auto, central, target, or all" >&2; exit 2 ;;
esac

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "update.sh must run as root (use sudo)" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

[[ -d "$REPO_DIR/.git" ]] || fail "$REPO_DIR is not a Git checkout"

want_central=false
want_target=false
case "$MODE" in
  central) want_central=true ;;
  target) want_target=true ;;
  all) want_central=true; want_target=true ;;
  auto)
    [[ -x "$CENTRAL_VENV/bin/python" || -e /etc/systemd/system/botman.service ]] && want_central=true
    [[ -x "$AGENT_VENV/bin/python" || -e /etc/systemd/system/botman-log-agent.service ]] && want_target=true
    ;;
esac
$want_central || $want_target || fail "no installed Botman component detected; use --mode explicitly or run setup.sh first"

owner=$(stat -c '%U' "$REPO_DIR")
run_git() {
  if [[ "$owner" == "root" ]]; then
    git -C "$REPO_DIR" "$@"
  else
    runuser -u "$owner" -- git -C "$REPO_DIR" "$@"
  fi
}

current_branch=$(run_git branch --show-current)
[[ "$current_branch" == "$BRANCH" ]] || fail "$REPO_DIR is on branch '$current_branch', expected '$BRANCH'"

if [[ -n "$(run_git status --porcelain --untracked-files=no)" ]]; then
  fail "$REPO_DIR has tracked local changes; commit/stash them before updating"
fi

old_commit=$(run_git rev-parse HEAD)
log "Fetching $BRANCH"
run_git fetch origin "$BRANCH"
run_git merge --ff-only "origin/$BRANCH"
new_commit=$(run_git rev-parse HEAD)
echo "Botman source: $old_commit -> $new_commit"

if $want_central; then
  [[ -x "$CENTRAL_VENV/bin/python" ]] || fail "central venv missing: $CENTRAL_VENV"
  log "Reinstalling central Botman"
  runuser -u botman -- "$CENTRAL_VENV/bin/python" -m pip install --upgrade "$REPO_DIR"
  runuser -u botman -- "$CENTRAL_VENV/bin/python" -m pip check
  cp "$REPO_DIR/systemd/botman.service" /etc/systemd/system/botman.service
fi

if $want_target; then
  [[ -x "$AGENT_VENV/bin/python" ]] || fail "agent venv missing: $AGENT_VENV"
  log "Reinstalling target agent/exporter"
  "$AGENT_VENV/bin/python" -m pip install --upgrade "$REPO_DIR[agent]"
  "$AGENT_VENV/bin/python" -m pip check
  cp "$REPO_DIR/systemd/botman-log-agent.service" /etc/systemd/system/botman-log-agent.service
fi

systemctl daemon-reload

if $want_target; then
  if [[ -s /var/lib/botman-log-agent/config.yaml ]]; then
    log "Restarting botman-log-agent.service"
    systemctl restart botman-log-agent.service
    systemctl --no-pager --full status botman-log-agent.service | sed -n '1,12p'
  else
    echo "Skipping agent start: /var/lib/botman-log-agent/config.yaml is not configured yet."
  fi
fi

if $want_central; then
  token=""
  if [[ -r /etc/botman/env ]]; then
    token=$(sed -n 's/^DISCORD_TOKEN=//p' /etc/botman/env | head -n1)
  fi
  if [[ -n "$token" ]]; then
    log "Restarting botman.service"
    systemctl restart botman.service
    systemctl --no-pager --full status botman.service | sed -n '1,12p'
  else
    echo "Skipping central start: DISCORD_TOKEN is empty/missing in /etc/botman/env."
  fi
fi

log "Update complete"
