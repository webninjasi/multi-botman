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
RUNTIME="auto"
TARGET_RUNTIME=""

usage() {
  cat <<'USAGE'
Usage: sudo scripts/setup.sh [options]

Install Botman using the canonical Git checkout + external-venv layout.
The target role detects an existing Docker or Podman runtime. Podman targets are
configured rootlessly; Docker targets grant botmgr access to the existing daemon.

Options:
  --mode central|target|all   Components to install (default: all)
  --repo-url URL             Git repository URL
  --branch NAME              Git branch to clone (default: main)
  --journal-max-use SIZE     journald SystemMaxUse (default: 1G)
  --runtime auto|docker|podman
                             Target runtime (default: auto-detect)
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
    --runtime) RUNTIME="${2:?missing value for --runtime}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MODE" in
  central|target|all) ;;
  *) echo "--mode must be central, target, or all" >&2; exit 2 ;;
esac

case "$RUNTIME" in
  auto|docker|podman) ;;
  *) echo "--runtime must be auto, docker, or podman" >&2; exit 2 ;;
esac

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "setup.sh must run as root (use sudo)" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

has_central() { [[ "$MODE" == "central" || "$MODE" == "all" ]]; }
has_target() { [[ "$MODE" == "target" || "$MODE" == "all" ]]; }

runtime_compose_works() {
  local runtime="$1"
  case "$runtime" in
    docker) command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 ;;
    podman) command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

detect_existing_runtime() {
  has_target || return 0

  if [[ "$RUNTIME" != "auto" ]]; then
    TARGET_RUNTIME="$RUNTIME"
    return 0
  fi

  local docker_installed=false podman_installed=false
  local docker_ok=false podman_ok=false
  command -v docker >/dev/null 2>&1 && docker_installed=true
  command -v podman >/dev/null 2>&1 && podman_installed=true
  runtime_compose_works docker && docker_ok=true
  runtime_compose_works podman && podman_ok=true

  if $docker_ok && $podman_ok; then
    fail "both Docker Compose and Podman Compose are installed; rerun with --runtime docker or --runtime podman"
  elif $docker_ok; then
    TARGET_RUNTIME="docker"
  elif $podman_ok; then
    TARGET_RUNTIME="podman"
  elif $docker_installed && ! $podman_installed; then
    # Keep an existing Docker installation even when only the Compose plugin is
    # missing. ensure_target_runtime() will install/verify Compose v2.
    TARGET_RUNTIME="docker"
  elif $podman_installed && ! $docker_installed; then
    TARGET_RUNTIME="podman"
  elif $docker_installed && $podman_installed; then
    fail "Docker and Podman are both installed but neither has a usable Compose command; rerun with --runtime docker or --runtime podman"
  fi
}

install_os_packages() {
  log "Installing OS prerequisites"
  if command -v dnf >/dev/null 2>&1; then
    local packages=(git openssh-clients ca-certificates sudo shadow-utils util-linux)
    if has_target; then
      packages+=(gcc systemd-devel)
    fi
    dnf install -y "${packages[@]}"
  elif command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    local packages=(git openssh-client ca-certificates sudo passwd util-linux python3-venv python3-pip)
    if has_target; then
      packages+=(build-essential libsystemd-dev python3-dev)
    fi
    apt-get install -y "${packages[@]}"
  else
    fail "unsupported package manager; install Git, OpenSSH, sudo, Python 3.11+, and libsystemd development headers manually"
  fi
}

install_docker_compose_plugin() {
  runtime_compose_works docker && return 0
  log "Docker is installed but Compose v2 is missing; installing a Compose plugin"

  if command -v apt-get >/dev/null 2>&1; then
    local package
    for package in docker-compose-plugin docker-compose-v2; do
      if apt-cache show "$package" >/dev/null 2>&1; then
        apt-get install -y "$package"
        runtime_compose_works docker && return 0
      fi
    done
  elif command -v dnf >/dev/null 2>&1; then
    local package
    for package in docker-compose-plugin docker-compose-v2; do
      if dnf -q list --available "$package" >/dev/null 2>&1 || dnf -q list --installed "$package" >/dev/null 2>&1; then
        dnf install -y "$package"
        runtime_compose_works docker && return 0
      fi
    done
  fi

  fail "Docker is installed but 'docker compose' is unavailable and no Compose v2 package was found in the configured repositories. Install Docker's Compose plugin for this distribution, then rerun setup"
}

ensure_target_runtime() {
  has_target || return 0

  if [[ -z "$TARGET_RUNTIME" ]]; then
    # Preserve the historical fresh-host default, but only attempt Podman when
    # the distribution actually advertises a package for it. Existing Docker
    # installations are detected before this point and are never replaced.
    if command -v dnf >/dev/null 2>&1 && dnf -q list --available podman >/dev/null 2>&1; then
      TARGET_RUNTIME="podman"
    elif command -v apt-cache >/dev/null 2>&1 && apt-cache show podman >/dev/null 2>&1; then
      TARGET_RUNTIME="podman"
    else
      fail "no usable Docker/Podman Compose runtime found. Install Docker with 'docker compose' or Podman with a Compose provider, then rerun setup (optionally with --runtime docker|podman)"
    fi
  fi

  case "$TARGET_RUNTIME" in
    podman)
      if ! command -v podman >/dev/null 2>&1; then
        log "Installing Podman runtime"
        if command -v dnf >/dev/null 2>&1; then
          dnf install -y podman
        elif command -v apt-get >/dev/null 2>&1; then
          apt-get install -y podman
        else
          fail "Podman is not installed"
        fi
      fi
      if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y uidmap dbus-user-session
      fi
      runtime_compose_works podman || fail "Podman is installed but 'podman compose' is unavailable; install a Compose provider"
      ;;
    docker)
      command -v docker >/dev/null 2>&1 || fail "--runtime docker requested, but Docker is not installed"
      install_docker_compose_plugin
      runtime_compose_works docker || fail "Docker Compose v2 is still unavailable after plugin installation"
      ;;
    *) fail "internal error: unresolved target runtime" ;;
  esac

  log "Target container runtime: $TARGET_RUNTIME"
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

install_supported_python() {
  find_python >/dev/null 2>&1 && return 0

  log "Python 3.11+ is not installed; installing a supported interpreter"

  if command -v apt-get >/dev/null 2>&1; then
    local version package
    for version in 3.13 3.12 3.11; do
      package="python${version}"
      if apt-cache show "$package" >/dev/null 2>&1; then
        apt-get install -y "$package" "${package}-venv" "${package}-dev"
        find_python >/dev/null 2>&1 && return 0
      fi
    done

    if [[ -r /etc/os-release ]]; then
      # shellcheck disable=SC1091
      . /etc/os-release
      if [[ "${ID:-}" == "ubuntu" ]]; then
        log "Ubuntu repositories do not provide Python 3.11+; enabling deadsnakes PPA"
        apt-get install -y software-properties-common
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update
        apt-get install -y python3.11 python3.11-venv python3.11-dev
        find_python >/dev/null 2>&1 && return 0
      fi
    fi
  elif command -v dnf >/dev/null 2>&1; then
    local version package
    for version in 3.13 3.12 3.11; do
      package="python${version}"
      if dnf -q list --available "$package" >/dev/null 2>&1 || dnf -q list --installed "$package" >/dev/null 2>&1; then
        dnf install -y "$package" "${package}-devel" || dnf install -y "$package"
        find_python >/dev/null 2>&1 && return 0
      fi
    done
  fi

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
  log "Configuring target host with $TARGET_RUNTIME"
  ensure_user "$MANAGEMENT_USER" /bin/bash "/home/$MANAGEMENT_USER"
  install -d -o "$MANAGEMENT_USER" -g "$MANAGEMENT_USER" -m 0700 "/home/$MANAGEMENT_USER/.ssh"
  install -d -o "$MANAGEMENT_USER" -g "$MANAGEMENT_USER" -m 0755 /srv/botman/stacks

  local uid runtime compose_command
  uid=$(id -u "$MANAGEMENT_USER")
  runtime="/run/user/$uid"

  if [[ "$TARGET_RUNTIME" == "podman" ]]; then
    ensure_subid_range "$MANAGEMENT_USER" /etc/subuid uid
    ensure_subid_range "$MANAGEMENT_USER" /etc/subgid gid
    start_user_manager "$MANAGEMENT_USER"
    runuser -u "$MANAGEMENT_USER" -- env XDG_RUNTIME_DIR="$runtime" podman info --format '{{.Host.Security.Rootless}}' | grep -qx true \
      || fail "Podman is not operating rootlessly for $MANAGEMENT_USER"
    runuser -u "$MANAGEMENT_USER" -- env XDG_RUNTIME_DIR="$runtime" podman compose version
    compose_command="podman compose"
  else
    local docker_group
    if ! docker info >/dev/null 2>&1 && systemctl list-unit-files docker.service >/dev/null 2>&1; then
      systemctl enable --now docker.service
    fi
    docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable; start/configure Docker and rerun setup"
    docker_group=$(stat -c '%G' /var/run/docker.sock 2>/dev/null || true)
    [[ -n "$docker_group" ]] || fail "Docker socket /var/run/docker.sock was not created"
    [[ "$docker_group" != "root" ]] || fail "Docker socket is group-owned by root; configure a dedicated Docker socket group before granting $MANAGEMENT_USER access"
    getent group "$docker_group" >/dev/null 2>&1 || fail "Docker socket group '$docker_group' does not exist"
    usermod -aG "$docker_group" "$MANAGEMENT_USER"
    runuser -u "$MANAGEMENT_USER" -- docker info >/dev/null \
      || fail "$MANAGEMENT_USER cannot access the Docker daemon; check /var/run/docker.sock permissions/group and Docker service status"
    runuser -u "$MANAGEMENT_USER" -- docker compose version
    compose_command="docker compose"
  fi

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
  if [[ "$TARGET_RUNTIME" == "podman" ]]; then
    runuser -u "$MANAGEMENT_USER" -- env XDG_RUNTIME_DIR="$runtime" \
      "$AGENT_VENV/bin/botman-target-preflight" \
      --management-user "$MANAGEMENT_USER" \
      --compose-command "$compose_command" \
      --journal-max-use "$JOURNAL_MAX_USE" || true
  else
    runuser -u "$MANAGEMENT_USER" -- \
      "$AGENT_VENV/bin/botman-target-preflight" \
      --management-user "$MANAGEMENT_USER" \
      --compose-command "$compose_command" \
      --journal-max-use "$JOURNAL_MAX_USE" || true
  fi
}

install_os_packages

# Create target identity before runtime setup so a recoverable runtime/plugin
# failure never leaves the host without the management account.
if has_target; then
  ensure_user "$MANAGEMENT_USER" /bin/bash "/home/$MANAGEMENT_USER"
fi

detect_existing_runtime
ensure_target_runtime
install_supported_python || fail "Python 3.11+ is required and setup could not install it; install Python 3.11+ manually or set PYTHON_BIN=/path/to/python"
PYTHON=$(find_python) || fail "Python 3.11+ detection failed after installation"
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
  echo "Target runtime: $TARGET_RUNTIME (configure compose_argv as: $TARGET_RUNTIME compose)"
  echo "Target: authorize the central SSH key for $MANAGEMENT_USER, register the server in Discord, then run /config agent sync."
fi
