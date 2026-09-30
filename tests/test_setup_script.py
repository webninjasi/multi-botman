from pathlib import Path


SETUP = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"


def _script() -> str:
    return SETUP.read_text(encoding="utf-8")


def test_setup_auto_detects_existing_docker_or_podman_without_unconditional_runtime_install() -> None:
    script = _script()
    assert 'RUNTIME="auto"' in script
    assert 'command -v docker' in script
    assert 'command -v podman' in script
    assert 'runtime_compose_works docker' in script
    assert 'runtime_compose_works podman' in script
    # The generic prerequisite transaction must not unconditionally request Podman.
    prerequisites = script.split("install_os_packages() {", 1)[1].split("install_docker_compose_plugin() {", 1)[0]
    assert "packages+=(podman" not in prerequisites


def test_setup_keeps_installed_docker_when_compose_plugin_is_missing() -> None:
    script = _script()
    detect = script.split("detect_existing_runtime() {", 1)[1].split("install_os_packages() {", 1)[0]
    assert 'docker_installed=false' in detect
    assert '$docker_installed && ! $podman_installed' in detect
    assert 'TARGET_RUNTIME="docker"' in detect
    assert "install_docker_compose_plugin" in script
    assert "docker-compose-plugin" in script
    assert "docker-compose-v2" in script


def test_setup_creates_management_user_before_runtime_plugin_setup() -> None:
    script = _script()
    main = script.rsplit("install_os_packages\n", 1)[1]
    create = 'ensure_user "$MANAGEMENT_USER" /bin/bash "/home/$MANAGEMENT_USER"'
    assert create in main
    assert main.index(create) < main.index("detect_existing_runtime")
    assert main.index(create) < main.index("ensure_target_runtime")


def test_setup_docker_target_uses_existing_daemon_not_podman_user_socket() -> None:
    script = _script()
    docker_branch = script.split('if [[ "$TARGET_RUNTIME" == "podman" ]]; then', 1)[1].split(
        'log "Configuring persistent journald"', 1
    )[0]
    assert 'docker_group=$(stat -c \'%G\' /var/run/docker.sock' in docker_branch
    assert 'usermod -aG "$docker_group" "$MANAGEMENT_USER"' in docker_branch
    assert 'runuser -u "$MANAGEMENT_USER" -- docker info' in docker_branch
    assert 'compose_command="docker compose"' in docker_branch


def test_setup_supports_explicit_runtime_override() -> None:
    script = _script()
    assert "--runtime auto|docker|podman" in script
    assert '--runtime) RUNTIME="${2:?missing value for --runtime}"' in script
    assert 'echo "--runtime must be auto, docker, or podman"' in script


def test_setup_installs_python_311_plus_instead_of_only_generic_python3() -> None:
    script = _script()
    assert "install_supported_python()" in script
    assert 'for version in 3.13 3.12 3.11' in script
    assert 'apt-get install -y "$package" "${package}-venv" "${package}-dev"' in script
    assert 'install_supported_python || fail' in script


def test_setup_has_ubuntu_fallback_when_stock_python_is_too_old() -> None:
    script = _script()
    assert '[[ "${ID:-}" == "ubuntu" ]]' in script
    assert 'add-apt-repository -y ppa:deadsnakes/ppa' in script
    assert 'apt-get install -y python3.11 python3.11-venv python3.11-dev' in script
