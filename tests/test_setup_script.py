from pathlib import Path


SETUP = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"


def _script() -> str:
    return SETUP.read_text(encoding="utf-8")


def test_setup_auto_detects_existing_docker_or_podman_before_installing_runtime() -> None:
    script = _script()
    assert 'RUNTIME="auto"' in script
    assert 'runtime_compose_works docker' in script
    assert 'runtime_compose_works podman' in script
    assert 'detect_existing_runtime\ninstall_os_packages\nensure_target_runtime' in script
    # The generic prerequisite transaction must not unconditionally request Podman.
    prerequisites = script.split("install_os_packages() {", 1)[1].split("ensure_target_runtime() {", 1)[0]
    assert "packages+=(podman" not in prerequisites


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
