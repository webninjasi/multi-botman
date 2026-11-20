"""Real Docker/Podman Compose acceptance for deployment and lifecycle commands.

Skipped unless ``BOTMAN_IT_COMPOSE`` names the Compose command, for example
``BOTMAN_IT_COMPOSE="podman compose"``. Each test builds a fresh two-app stack
under ``/srv/botman/stacks`` (which must exist and be writable) through a local
server, pulls ``BOTMAN_IT_BASE_IMAGE``, and logs to journald. CI runs this from
``.github/workflows/runtime-integration.yml``.

Git is the only stand-in: commits come from local fixture repos instead of an
SSH remote. Archive, upload, checksum, release switch, build, and up are real.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from botman.compose import (
    AppDisabledError,
    ComposeAdminService,
    LifecycleOperation,
    LifecycleService,
)
from botman.deployment import DeploymentError, DeploymentResult, DeploymentService
from botman.executor import ExecResult
from botman.git import GitRevision
from botman.models import STACK_BASE, BotmanConfig
from botman.routing import ResolvedApp

COMPOSE_ARGV = tuple(shlex.split(os.environ.get("BOTMAN_IT_COMPOSE", "")))
RUNTIME = os.environ.get("BOTMAN_IT_RUNTIME") or (COMPOSE_ARGV[0] if COMPOSE_ARGV else "")
JOURNALCTL = tuple(shlex.split(os.environ.get("BOTMAN_IT_JOURNALCTL", "journalctl")))
BASE_IMAGE = os.environ.get("BOTMAN_IT_BASE_IMAGE", "docker.io/library/busybox:1.36")
CHANNEL = "111"
SERVICES = {"app-a": "svc-a", "app-b": "svc-b"}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not COMPOSE_ARGV, reason="set BOTMAN_IT_COMPOSE to run runtime integration"),
]

RUN_SH = """\
trap 'exit 0' TERM
echo "botman-it version=$(cat /app/version.txt)"
while :; do sleep 1; done
"""


def _run(argv: tuple[str, ...], *, cwd: Path | None = None, timeout: float = 120) -> str:
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{shlex.join(argv)} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def _exists(argv: tuple[str, ...]) -> bool:
    return subprocess.run(argv, capture_output=True, timeout=60, check=False).returncode == 0


def _git(repo: Path, *args: str) -> str:
    return _run(
        ("git", "-c", "user.name=botman-it", "-c", "user.email=it@botman.invalid", *args),
        cwd=repo,
    )


def _output(result: ExecResult | None) -> str:
    return "" if result is None else f"{result.stdout}\n{result.stderr}"


def _ok(result: ExecResult) -> ExecResult:
    assert result.ok, f"{shlex.join(result.argv)}\n{result.stdout}\n{result.stderr}"
    return result


class LocalRepoGit:
    """GitRepositoryManager stand-in backed by local fixture repositories."""

    def __init__(self, repos: dict[str, Path]):
        self.repos = repos

    async def fetch(self, resolved: ResolvedApp) -> GitRevision:
        repo = self.repos[resolved.name]
        return GitRevision(sha=_git(repo, "rev-parse", "HEAD"), repo_path=repo)

    async def archive(self, resolved: ResolvedApp, sha: str, destination: Path) -> None:
        _git(self.repos[resolved.name], "archive", "--format=tar", "-o", str(destination), sha)


@dataclass(frozen=True)
class ContainerState:
    id: str
    running: bool
    started_at: str


@dataclass
class Stack:
    name: str
    config: BotmanConfig
    repos: dict[str, Path]
    last_apply: ExecResult | None = None

    @property
    def project(self) -> str:
        return self.config.stacks[self.name].project_name

    @property
    def root(self) -> Path:
        return Path(str(self.config.stack_root(self.name)))

    def log_identifier(self, app: str) -> str:
        return self.config.stacks[self.name].apps[app].log_identifier

    def compose(
        self,
        *,
        app_a_environment: dict[str, str] | None = None,
        without: tuple[str, ...] = (),
        extra: bool = False,
    ) -> str:
        services: dict[str, dict] = {}
        for app, service in SERVICES.items():
            if service in without:
                continue
            services[service] = {
                "build": {
                    "context": f"./apps/{app}/current",
                    "args": {"GREETING": "${GREETING:-unset}"},
                },
                "logging": {
                    "driver": "journald",
                    "options": {"tag": self.log_identifier(app)},
                },
                "stop_grace_period": "5s",
            }
        if "svc-a" in services:
            services["svc-a"]["volumes"] = ["data:/data"]
        if app_a_environment:
            services["svc-a"]["environment"] = app_a_environment
        if extra:
            # A service that is not a managed app, like a database.
            services["extra"] = {
                "image": BASE_IMAGE,
                "command": ["sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"],
                "stop_grace_period": "5s",
            }
        return yaml.safe_dump({"services": services, "volumes": {"data": {}}}, sort_keys=False)

    def commit(self, app: str, version: str, *, broken: bool = False) -> str:
        repo = self.repos[app]
        dockerfile = [
            f"FROM {BASE_IMAGE}",
            "ARG GREETING=unset",
            "COPY version.txt run.sh /app/",
            'RUN echo "$GREETING" > /app/greeting.txt',
        ]
        if broken:
            dockerfile.append("RUN echo 'deliberately broken build' >&2 && exit 1")
        dockerfile.append('CMD ["sh", "/app/run.sh"]')
        (repo / "Dockerfile").write_text("\n".join(dockerfile) + "\n")
        (repo / "run.sh").write_text(RUN_SH)
        (repo / "version.txt").write_text(version + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", version)
        return _git(repo, "rev-parse", "HEAD")

    async def deploy(self, app: str) -> DeploymentResult:
        service = DeploymentService(self.config, git=LocalRepoGit(self.repos))  # type: ignore[arg-type]
        try:
            return await service.update(app_name=app, channel_id=CHANNEL)
        except DeploymentError as exc:
            exc.add_note(exc.transcript.render().decode())
            raise

    async def lifecycle(self, operation: LifecycleOperation, app: str) -> ExecResult:
        result = await LifecycleService(self.config).execute(
            operation, app_name=app, channel_id=CHANNEL
        )
        return _ok(result)

    async def upload_compose(self, content: str) -> None:
        _ok(await ComposeAdminService(self.config).upload(self.name, content))

    async def compose_apply(self):
        service = ComposeAdminService(self.config)
        plan = await service.plan_apply(self.name)
        self.last_apply = _ok(await service.apply(self.name, plan))
        return plan

    def current(self, app: str) -> str:
        return os.readlink(self.root / "apps" / app / "current")

    def container(self, app: str) -> ContainerState:
        matches = self.containers(SERVICES[app])
        assert len(matches) == 1, f"expected one {SERVICES[app]} container, got {matches}"
        return matches[0]

    def containers(self, service: str) -> list[ContainerState]:
        """Look containers up by Compose labels so any provider works."""

        ids = _run(
            (
                RUNTIME, "ps", "-a", "--no-trunc",
                "--filter", f"label=com.docker.compose.project={self.project}",
                "--format", "{{.ID}}",
            )
        ).split()
        matches = []
        for container_id in ids:
            fields = _run(
                (
                    RUNTIME, "inspect", "--format",
                    '{{index .Config.Labels "com.docker.compose.service"}}'
                    " {{.State.Running}} {{.State.StartedAt}}",
                    container_id,
                )
            ).split(" ", 2)
            if fields[0] == service:
                matches.append(ContainerState(container_id, fields[1] == "true", fields[2]))
        return matches

    def exec(self, app: str, *argv: str) -> str:
        return _run((RUNTIME, "exec", self.container(app).id, *argv))

    def teardown(self) -> None:
        compose_file = str(self.config.compose_path(self.name))
        subprocess.run(
            (*COMPOSE_ARGV, "-p", self.project, "-f", compose_file, "down", "-v"),
            capture_output=True, timeout=180, check=False,
        )
        shutil.rmtree(self.root, ignore_errors=True)


def _config(name: str) -> BotmanConfig:
    project = f"botman-{name}"
    return BotmanConfig.model_validate(
        {
            "servers": {"target": {"type": "local", "compose_argv": list(COMPOSE_ARGV)}},
            "stacks": {
                name: {
                    "server": "target",
                    "channel_id": CHANNEL,
                    "project_name": project,
                    "apps": {
                        app: {
                            "service": service,
                            "log_identifier": f"{project}-{app}",
                            "git": {"repo_url": f"git@example.invalid:it/{app}.git"},
                        }
                        for app, service in SERVICES.items()
                    },
                }
            },
        }
    )


@pytest.fixture
async def stack(tmp_path: Path):
    """Upload the Compose file, then `/update` both apps to their v1 commit."""

    base = Path(str(STACK_BASE))
    if not (base.is_dir() and os.access(base, os.W_OK)):
        pytest.fail(f"{base} must exist and be writable by the test user")
    name = f"it-{uuid.uuid4().hex[:8]}"
    repos = {}
    for app in SERVICES:
        repos[app] = tmp_path / app
        repos[app].mkdir()
        _git(repos[app], "init", "-q", "-b", "main")
    stack = Stack(name, _config(name), repos)
    try:
        await stack.upload_compose(stack.compose())
        for app in SERVICES:
            sha = stack.commit(app, "v1")
            result = await stack.deploy(app)
            assert (result.status, result.sha, result.previous_sha) == ("deployed", sha, None)
            assert stack.container(app).running
        yield stack
    finally:
        stack.teardown()


async def test_update_replaces_only_the_target_app_and_keeps_its_volume(stack: Stack) -> None:
    v1 = stack.current("app-a")
    b_before = stack.container("app-b")
    stack.exec("app-a", "sh", "-c", "echo keep > /data/marker")

    sha = stack.commit("app-a", "v2")
    result = await stack.deploy("app-a")

    assert (result.status, result.sha, f"releases/{result.previous_sha}") == ("deployed", sha, v1)
    assert stack.current("app-a") == f"releases/{sha}"
    assert stack.container("app-a").running
    assert stack.exec("app-a", "cat", "/app/version.txt") == "v2"
    assert stack.exec("app-a", "cat", "/data/marker") == "keep"
    assert stack.container("app-b") == b_before


async def test_update_to_the_running_commit_is_a_noop(stack: Stack) -> None:
    before = stack.container("app-a")

    result = await stack.deploy("app-a")

    assert result.status == "noop"
    assert stack.container("app-a") == before


async def test_failed_build_restores_current_and_leaves_the_app_running(stack: Stack) -> None:
    previous = stack.current("app-a")
    before = stack.container("app-a")
    stack.commit("app-a", "v2", broken=True)

    with pytest.raises(DeploymentError, match="Compose build failed"):
        await stack.deploy("app-a")

    assert stack.current("app-a") == previous
    assert stack.container("app-a") == before
    assert stack.exec("app-a", "cat", "/app/version.txt") == "v1"


async def test_lifecycle_commands_touch_only_the_requested_service(stack: Stack) -> None:
    b_before = stack.container("app-b")

    await stack.lifecycle("stop", "app-a")
    assert not stack.container("app-a").running
    await stack.lifecycle("status", "app-a")

    await stack.lifecycle("start", "app-a")
    started = stack.container("app-a")
    assert started.running

    await stack.lifecycle("restart", "app-a")
    restarted = stack.container("app-a")
    assert restarted.running
    assert restarted.started_at != started.started_at

    assert stack.container("app-b") == b_before


async def test_rebuild_bakes_build_args_from_the_app_env_file(stack: Stack) -> None:
    assert stack.exec("app-a", "cat", "/app/greeting.txt") == "unset"
    b_before = stack.container("app-b")
    env_file = Path(str(stack.config.app_env_path(stack.name, "app-a")))
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("GREETING=from-env\n")

    await stack.lifecycle("rebuild", "app-a")

    assert stack.exec("app-a", "cat", "/app/greeting.txt") == "from-env"
    assert stack.container("app-b") == b_before


async def test_apply_recreates_the_app_with_an_edited_compose_file(stack: Stack) -> None:
    b_before = stack.container("app-b")
    await stack.upload_compose(stack.compose(app_a_environment={"BOTMAN_IT_FLAG": "on"}))

    await stack.lifecycle("apply", "app-a")

    assert stack.exec("app-a", "sh", "-c", "echo $BOTMAN_IT_FLAG") == "on"
    assert stack.container("app-b") == b_before


async def test_container_output_reaches_journald_under_the_log_identifier(stack: Stack) -> None:
    argv = (
        *JOURNALCTL, "--no-pager", "-o", "cat",
        f"SYSLOG_IDENTIFIER={stack.log_identifier('app-a')}",
    )
    deadline = time.monotonic() + 30
    output = ""
    while time.monotonic() < deadline:
        output = subprocess.run(argv, capture_output=True, text=True, check=False).stdout
        if "botman-it version=v1" in output:
            break
        time.sleep(1)
    assert "botman-it version=v1" in output, output


async def test_compose_apply_adds_new_services_and_removes_dropped_ones(stack: Stack) -> None:
    apps_before = {app: stack.container(app) for app in SERVICES}
    await stack.upload_compose(stack.compose(extra=True))

    plan = await stack.compose_apply()

    assert plan.services == ("svc-a", "svc-b", "extra")
    assert plan.orphans == ()
    [extra] = stack.containers("extra")
    assert extra.running
    # `up` leaves services whose config did not change alone.
    output = _output(stack.last_apply)
    await stack.compose_apply()
    again = _output(stack.last_apply)
    assert stack.containers("extra") == [extra], again
    assert {app: stack.container(app) for app in SERVICES} == apps_before, output

    await stack.upload_compose(stack.compose())
    plan = await stack.compose_apply()

    assert plan.orphans == ("extra",)
    assert stack.containers("extra") == []
    assert {app: stack.container(app) for app in SERVICES} == apps_before


async def test_app_removed_from_compose_is_disabled_but_keeps_its_release(stack: Stack) -> None:
    a_before = stack.container("app-a")
    release = stack.current("app-b")
    await stack.upload_compose(stack.compose(without=("svc-b",)))

    plan = await stack.compose_apply()

    assert plan.orphans == ("svc-b",)
    assert stack.containers("svc-b") == []
    assert stack.container("app-a") == a_before
    with pytest.raises(AppDisabledError, match="`app-b` is disabled"):
        await stack.lifecycle("start", "app-b")
    assert stack.current("app-b") == release

    await stack.upload_compose(stack.compose())
    await stack.compose_apply()

    assert stack.container("app-b").running


async def test_app_remove_deletes_a_disabled_apps_container_image_and_files(
    stack: Stack, tmp_path: Path
) -> None:
    from botman.app_removal import AppRemovalService
    from botman.config import ConfigStore

    a_before = stack.container("app-a")
    env_file = Path(str(stack.config.app_env_path(stack.name, "app-b")))
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("KEY=value\n")
    images = [f"{stack.project}-svc-b", f"{stack.project}_svc-b"]
    assert any(_exists((RUNTIME, "image", "inspect", image)) for image in images)
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(stack.config)
    # Disable app-b without applying, so removal also has to remove its container.
    await stack.upload_compose(stack.compose(without=("svc-b",)))
    service = AppRemovalService(store)

    plan = await service.plan(app_name="app-b", channel_id=CHANNEL)
    result = _ok(await service.remove(app_name="app-b", channel_id=CHANNEL, expected=plan))

    assert stack.containers("svc-b") == [], result.stdout
    assert not any(_exists((RUNTIME, "image", "inspect", image)) for image in images), result.stdout
    assert not (stack.root / "apps" / "app-b").exists()
    assert not env_file.exists()
    assert "app-b" not in store.load().stacks[stack.name].apps
    assert stack.container("app-a") == a_before


async def test_compose_prune_removes_dropped_networks_and_only_opted_in_volumes(
    stack: Stack,
) -> None:
    def with_extra_resources(content: str) -> str:
        data = yaml.safe_load(content)
        data["services"]["extra"]["volumes"] = ["cache:/cache"]
        data["services"]["extra"]["networks"] = ["side"]
        data["volumes"]["cache"] = {}
        data["networks"] = {"side": {}}
        return yaml.safe_dump(data, sort_keys=False)

    await stack.upload_compose(with_extra_resources(stack.compose(extra=True)))
    await stack.compose_apply()
    network, volume = f"{stack.project}_side", f"{stack.project}_cache"
    assert _exists((RUNTIME, "network", "inspect", network))
    assert _exists((RUNTIME, "volume", "inspect", volume))
    await stack.upload_compose(stack.compose())
    await stack.compose_apply()
    service = ComposeAdminService(stack.config)

    plan = await service.plan_prune(stack.name, include_volumes=False)
    assert (plan.networks, plan.volumes) == ((network,), (volume,))
    _ok(await service.prune(stack.name, plan))
    assert not _exists((RUNTIME, "network", "inspect", network))
    assert _exists((RUNTIME, "volume", "inspect", volume))

    plan = await service.plan_prune(stack.name, include_volumes=True)
    _ok(await service.prune(stack.name, plan))
    assert not _exists((RUNTIME, "volume", "inspect", volume))
    assert _exists((RUNTIME, "volume", "inspect", f"{stack.project}_data"))
    assert stack.container("app-a").running
