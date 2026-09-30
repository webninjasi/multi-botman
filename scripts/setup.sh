#!/usr/bin/env bash
set -euo pipefail

MODE="all"
REPO_URL="https://github.com/webninjasi/multi-botman.git"
BRANCH="main"
REPO_DIR="/opt/botman"
CENTRAL_VENV="/opt/botman-venv"
AGENT_VENV="/opt/botman-agent-venv"
CENTRAL_USER="botman"
MANAGEMENT_USER="botmgr"
AGENT_USER="botman-log-agent"
JOURNAL_MAX_USE="1G"

usage() {
  cat <<'USAGE'
Usage: sudo scripts/setup.sh [options]

Install Botman using the canonical Git checkout + external-venv layout.
The target role configures rootless Podman for botmgr.

Options:
  --mode central|target|all   Components to install (default: all)
  --repo-url URL             Git repository URL
  --branch NAME              Git branch to clone (default: main)
  --journal-max-use SIZE     journald SystemMaxUse (default: 1G)
  -h, --help                 Show this help

Fresh-host examples:
  sudo ./scripts/setup.sh --mode all
  sudo ./scripts/setup.sh --mode target

The script does not create SSH trust/deploy keys and does not invent Discord
secrets. After central setup, edit /etc/botman/env. After target setup, add the
central SSH public key to botmgr and run /config agent sync from Discord.
USAGE
}

while (($#)); do
  case "$1" in
    --mode) MODE="${2:?missing value for --mode}"; shift 2 ;;
    --repo-url) REPO_URL="${2:?missing value for --repo-url}"; shift 2 ;;
    --branch) BRANCH="${2:?missing value for --branch}"; shift 2 ;;
    --journal-max-use) JOURNAL_MAX_USE="${2:?missing value for --journal-max-use}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MODE" in
  central|target|all) ;;
  *) echo "--mode must be central, target, or all" >&2; exit 2 ;;
esac

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "setup.sh must run as root (use sudo)" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

has_central() { [[ "$MODE" == "central" || "$MODE" == "all" ]]; }
has_target() { [[ "$MODE" == "target" || "$MODE" == "all" ]]; }

install_os_packages() {
  log "Installing OS prerequisites"
  if command -v dnf >/dev/null 2>&1; then
    local packages=(git openssh-clients ca-certificates sudo shadow-utils util-linux)
    if has_target; then
      packages+=(podman gcc systemd-devel)
    fi
    dnf install -y "${packages[@]}"
  elif command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    local packages=(git openssh-client ca-certificates sudo passwd util-linux python3-venv python3-pip)
    if has_target; then
      packages+=(podman uidmap dbus-user-session build-essential libsystemd-dev python3-dev)
    fi
    apt-get install -y "${packages[@]}"
  else
    fail "unsupported package manager; install Git, OpenSSH, sudo, Python 3.11+, and target Podman/systemd development packages manually"
  fi
}

find_python() {
  local candidate
  for candidate in "${PYTHON_BIN:-}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$candidate" ]] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_user() {
  local user="$1" shell="$2" home="$3"
  if ! id "$user" >/dev/null 2>&1; then
    useradd --create-home --home-dir "$home" --shell "$shell" "$user"
  fi
}

ensure_subid_range() {
  local user="$1" file="$2" kind="$3"
  grep -q "^${user}:" "$file" 2>/dev/null && return 0

  local start
  start=$(awk -F: '
    BEGIN { max=100000 }
    NF >= 3 { end=$2+$3; if (end > max) max=end }
    END {
      block=65536
      aligned=int((max+block-1)/block)*block
      if (aligned < 100000) aligned=100000
      print aligned
    }
  ' "$file" 2>/dev/null || echo 100000)
  local end=$((start + 65535))
  if [[ "$kind" == "uid" ]]; then
    usermod --add-subuids "${start}-${end}" "$user"
  else
    usermod --add-subgids "${start}-${end}" "$user"
  fi
}

repo_owner() {
  if [[ -d "$REPO_DIR/.git" ]]; then
    stat -c '%U' "$REPO_DIR"
  elif has_central; then
    echo "$CENTRAL_USER"
  else
    echo root
  fi
}

run_as_owner() {
  local owner="$1"; shift
  if [[ "$owner" == "root" ]]; then
    "$@"
  else
    runuser -u "$owner" -- "$@"
  fi
}

ensure_repo() {
  local owner="$1"
  if [[ -d "$REPO_DIR/.git" ]]; then
    log "Using existing Git checkout at $REPO_DIR"
    return 0
  fi
  if [[ -e "$REPO_DIR" ]] && [[ -n "$(find "$REPO_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    fail "$REPO_DIR exists and is not a Git checkout"
  fi
  rm -rf "$REPO_DIR"
  install -d -o "$owner" -g "$owner" -m 0755 "$REPO_DIR"
  log "Cloning $REPO_URL ($BRANCH) into $REPO_DIR"
  run_as_owner "$owner" git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$REPO_DIR"
}

ensure_venv() {
  local path="$1" owner="$2" python="$3" requirement="$4"
  if [[ ! -x "$path/bin/python" ]]; then
    rm -rf "$path"
    install -d -o "$owner" -g "$owner" -m 0755 "$path"
    run_as_owner "$owner" "$python" -m venv "$path"
  fi
  run_as_owner "$owner" "$path/bin/python" -m pip install --upgrade pip setuptools wheel
  run_as_owner "$owner" "$path/bin/python" -m pip install --upgrade "$requirement"
}

setup_central() {
  log "Configuring central Botman"
  ensure_user "$CENTRAL_USER" /bin/bash "/home/$CENTRAL_USER"
  install -d -o "$CENTRAL_USER" -g "$CENTRAL_USER" -m 0700 "/home/$CENTRAL_USER/.ssh"
  install -d -o "$CENTRAL_USER" -g "$CENTRAL_USER" -m 0700 /var/lib/botman /var/lib/botman/repos /var/lib/botman/keys /etc/botman

  local owner
  owner=$(repo_owner)
  ensure_repo "$owner"
  chown -R "$CENTRAL_USER:$CENTRAL_USER" "$REPO_DIR"
  owner="$CENTRAL_USER"

  ensure_venv "$CENTRAL_VENV" "$CENTRAL_USER" "$PYTHON" "$REPO_DIR"

  if [[ ! -e /etc/botman/env ]]; then
    cat >/etc/botman/env <<'ENV'
DISCORD_TOKEN=
ADMIN_IDS=
BOTMAN_CONFIG=/etc/botman/config.yaml
ENV
  fi
  chown "$CENTRAL_USER:$CENTRAL_USER" /etc/botman/env
  chmod 0600 /etc/botman/env

  touch /etc/botman/git_known_hosts
  chown "$CENTRAL_USER:$CENTRAL_USER" /etc/botman/git_known_hosts
  chmod 0600 /etc/botman/git_known_hosts

  cp "$REPO_DIR/systemd/botman.service" /etc/systemd/system/botman.service
  systemctl daemon-reload
  systemctl enable botman.service

  local token
  token=$(sed -n 's/^DISCORD_TOKEN=//p' /etc/botman/env | head -n1)
  if [[ -n "$token" ]]; then
    systemctl restart botman.service
  else
    echo "Central service installed but not started: set DISCORD_TOKEN and ADMIN_IDS in /etc/botman/env first."
  fi
}

start_user_manager() {
  local user="$1" uid runtime
  uid=$(id -u "$user")
  runtime="/run/user/$uid"

  loginctl enable-linger "$user"
  systemctl start "user@${uid}.service"

  [[ -S "$runtime/bus" ]] || fail "user manager bus was not created at $runtime/bus"

  runuser -u "$user" -- env \
    XDG_RUNTIME_DIR="$runtime" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" \
    systemctl --user enable --now podman.socket

  [[ -S "$runtime/podman/podman.sock" ]] || fail "rootless Podman socket was not created at $runtime/podman/podman.sock"
}

setup_target() {
  log "Configuring target host with rootless Podman"
  ensure_user "$MANAGEMENT_USER" /bin/bash "/home/$MANAGEMENT_USER"
  install -d -o "$MANAGEMENT_USER" -g "$MANAGEMENT_USER" -m 0700 "/home/$MANAGEMENT_USER/.ssh"
  install -d -o "$MANAGEMENT_USER" -g "$MANAGEMENT_USER" -m 0755 /srv/botman/stacks

  ensure_subid_range "$MANAGEMENT_USER" /etc/subuid uid
  ensure_subid_range "$MANAGEMENT_USER" /etc/subgid gid
  start_user_manager "$MANAGEMENT_USER"

  local uid runtime
  uid=$(id -u "$MANAGEMENT_USER")
  runtime="/run/user/$uid"
  runuser -u "$MANAGEMENT_USER" -- env XDG_RUNTIME_DIR="$runtime" podman info --format '{{.Host.Security.Rootless}}' | grep -qx true \
    || fail "Podman is not operating rootlessly for $MANAGEMENT_USER"
  runuser -u "$MANAGEMENT_USER" -- env XDG_RUNTIME_DIR="$runtime" podman compose version

  log "Configuring persistent journald"
  install -d -m 0755 /etc/systemd/journald.conf.d
  cat >/etc/systemd/journald.conf.d/90-botman.conf <<EOF2
[Journal]
Storage=persistent
SystemMaxUse=$JOURNAL_MAX_USE
Compress=yes
EOF2
  systemctl restart systemd-journald

  if ! id "$AGENT_USER" >/dev/null 2>&1; then
    local nologin_shell
    nologin_shell=$(command -v nologin || true)
    [[ -n "$nologin_shell" ]] || nologin_shell=/sbin/nologin
    useradd --system --home-dir /var/lib/botman-log-agent --create-home --shell "$nologin_shell" "$AGENT_USER"
  fi
  usermod -aG systemd-journal "$AGENT_USER"
  usermod -aG "$AGENT_USER",systemd-journal "$MANAGEMENT_USER"

  chown root:"$AGENT_USER" /var/lib/botman-log-agent
  chmod 3770 /var/lib/botman-log-agent
  install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0700 /var/lib/botman-log-agent/state

  local owner
  owner=$(repo_owner)
  ensure_repo "$owner"
  ensure_venv "$AGENT_VENV" root "$PYTHON" "$REPO_DIR[agent]"

  cp "$REPO_DIR/systemd/botman-log-agent.service" /etc/systemd/system/botman-log-agent.service
  systemctl daemon-reload
  systemctl enable botman-log-agent.service

  local systemctl_path
  systemctl_path=$(command -v systemctl)
  cat >/etc/sudoers.d/botman-log-agent-control <<EOF2
$MANAGEMENT_USER ALL=(root) NOPASSWD: $systemctl_path restart botman-log-agent.service, $systemctl_path is-active botman-log-agent.service, $systemctl_path status --no-pager --lines=20 botman-log-agent.service
EOF2
  chmod 0440 /etc/sudoers.d/botman-log-agent-control
  visudo -cf /etc/sudoers.d/botman-log-agent-control >/dev/null

  if [[ -s /var/lib/botman-log-agent/config.yaml ]]; then
    systemctl restart botman-log-agent.service
  else
    echo "Agent service installed/enabled but not started: run /config agent sync after server/app configuration."
  fi

  log "Running target preflight"
  runuser -u "$MANAGEMENT_USER" -- env XDG_RUNTIME_DIR="$runtime" \
    "$AGENT_VENV/bin/botman-target-preflight" \
    --management-user "$MANAGEMENT_USER" \
    --compose-command "podman compose" \
    --journal-max-use "$JOURNAL_MAX_USE" || true
}

install_os_packages
PYTHON=$(find_python) || fail "Python 3.11+ is required; install it and rerun (or set PYTHON_BIN=/path/to/python)"
log "Using Python: $PYTHON ($($PYTHON --version 2>&1))"

if has_target && command -v dnf >/dev/null 2>&1; then
  pybase=$(basename "$PYTHON")
  case "$pybase" in
    python3.11|python3.12|python3.13)
      dnf install -y "${pybase}-devel" || dnf install -y python3-devel
      ;;
    *)
      dnf install -y python3-devel
      ;;
  esac
fi

# Create the central user before selecting checkout ownership in all/central mode.
if has_central; then
  ensure_user "$CENTRAL_USER" /bin/bash "/home/$CENTRAL_USER"
fi

if has_central; then setup_central; fi
if has_target; then setup_target; fi

log "Setup complete"
if has_central; then
  echo "Central: edit /etc/botman/env, configure SSH/Git known_hosts, then start/restart botman.service."
fi
if has_target; then
  echo "Target: authorize the central SSH key for $MANAGEMENT_USER, register the server in Discord, then run /config agent sync."
fi
