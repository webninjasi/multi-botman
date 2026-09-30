from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from botman.compose import ComposeManager, StackLockRegistry
from botman.deployment import DeploymentError, DeploymentService
from botman.executor import ExecResult
from botman.git import GitRevision
from botman.models import BotmanConfig
from botman.routing import ChannelAuthorizationError, resolve_app


SHA1 = "1" * 40
SHA2 = "2" * 40


def config() -> BotmanConfig:
    return BotmanConfig.model_validate(
        {
            "settings": {"release_keep_count": 2},
            "servers": {"target": {"type": "local", "compose_argv": ["docker", "compose"]}},
            "stacks": {
                "bots": {
                    "server": "target",
                    "channel_id": "111",
                    "project_name": "botman-bots",
                }
            },
            "apps": {
                "app-a": {
                    "stack": "bots",
                    "service": "svc-a",
                    "log_identifier": "botman-bots-app-a",
                    "git": {
                        "repo_url": "git@example.com:o/a.git",
                        "deploy_key_path": "/tmp/deploy-app-a",
                    },
                },
                "app-b": {
                    "stack": "bots",
                    "service": "svc-b",
                    "log_identifier": "botman-bots-app-b",
                    "git": {
                        "repo_url": "git@example.com:o/b.git",
                        "deploy_key_path": "/tmp/deploy-app-b",
                    },
                },
            },
        }
    )


class FakeGit:
    def __init__(self, sha: str = SHA2) -> None:
        self.sha = sha
        self.fetch_calls = 0
        self.archive_calls = 0

    async def fetch(self, app_name: str):
        self.fetch_calls += 1
        return GitRevision(self.sha, Path("/tmp/fake.git"))

    async def archive(self, app_name: str, sha: str, destination: Path):
        self.archive_calls += 1
        with tarfile.open(destination, "w") as archive:
            data = b"print('hello')\n"
            info = tarfile.TarInfo("app.py")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


class FakeTargetExecutor:
    def __init__(
        self,
        *,
        current: str | None = None,
        checksum_mismatch: bool = False,
        build_codes: list[int] | None = None,
        up_codes: list[int] | None = None,
        extract_code: int = 0,
    ) -> None:
        self.current = current
        self.checksum_mismatch = checksum_mismatch
        self.build_codes = list(build_codes or [0])
        self.up_codes = list(up_codes or [0])
        self.extract_code = extract_code
        self.files: dict[str, bytes] = {}
        self.links: dict[str, str] = {}
        self.releases: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    async def upload(self, local_path, remote_path):
        self.files[str(remote_path)] = Path(local_path).read_bytes()

    async def write_bytes(self, path, data, *, mode=0o600, atomic=True):
        self.files[str(path)] = data

    async def run(self, argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        self.calls.append(args)
        rc = 0
        out = ""
        err = ""

        if args[0] == "readlink":
            if self.current is None:
                rc = 1
            else:
                out = self.current + "\n"
        elif args[0] == "sha256sum":
            data = self.files[args[-1]]
            digest = hashlib.sha256(data).hexdigest()
            if self.checksum_mismatch:
                digest = "f" * 64
            out = f"{digest}  {args[-1]}\n"
        elif args[0] == "tar":
            rc = self.extract_code
            if rc:
                err = "extract failed"
        elif args[:2] == ("ln", "-s"):
            self.links[args[-1]] = args[-2]
        elif args[:2] == ("mv", "-Tf"):
            temp, current_path = args[-2:]
            self.current = self.links.pop(temp)
        elif args[0] == "mv" and "-Tf" not in args:
            src, dst = args[-2:]
            if "/releases/." in src:
                self.releases.add(Path(dst).name)
        elif args[0] == "rm" and args[1] == "-f":
            target = args[-1]
            self.files.pop(target, None)
            self.links.pop(target, None)
            if target.endswith("/current"):
                self.current = None
        elif args[0] == "rm" and args[1] == "-rf":
            name = Path(args[-1]).name
            self.releases.discard(name)
        elif args[0] == "find":
            out = "\n".join(
                f"{1000 + i}.0 {sha}" for i, sha in enumerate(sorted(self.releases))
            )
            if out:
                out += "\n"
        elif args[:2] == ("docker", "compose") and "build" in args:
            rc = self.build_codes.pop(0) if self.build_codes else 0
            if rc:
                err = "build failed"
        elif args[:2] == ("docker", "compose") and "up" in args:
            rc = self.up_codes.pop(0) if self.up_codes else 0
            if rc:
                err = "up failed"

        result = ExecResult(args, rc, out, err)
        return result.check() if check else result


@pytest.mark.asyncio
async def test_same_sha_is_noop_before_archive_or_compose() -> None:
    cfg = config()
    git = FakeGit(SHA1)
    target = FakeTargetExecutor(current=f"releases/{SHA1}")
    service = DeploymentService(cfg, git=git, executor_factory=lambda _: target)

    result = await service.update(app_name="app-a", channel_id=111)

    assert result.status == "noop"
    assert git.fetch_calls == 1
    assert git.archive_calls == 0
    assert not any(call[:2] == ("docker", "compose") for call in target.calls)


@pytest.mark.asyncio
async def test_successful_update_targets_only_requested_service() -> None:
    cfg = config()
    git = FakeGit(SHA2)
    target = FakeTargetExecutor(current=f"releases/{SHA1}")
    target.releases.add(SHA1)
    service = DeploymentService(cfg, git=git, executor_factory=lambda _: target)

    result = await service.update(app_name="app-a", channel_id="111")

    assert result.status == "deployed"
    assert result.sha == SHA2
    assert result.previous_sha == SHA1
    assert target.current == f"releases/{SHA2}"
    compose_calls = [call for call in target.calls if call[:2] == ("docker", "compose")]
    assert [call[-2:] for call in compose_calls] == [("build", "svc-a"), ("--no-deps", "svc-a")]
    assert all("svc-b" not in call for call in compose_calls)
    assert b"deployment active" in result.transcript


@pytest.mark.asyncio
async def test_checksum_mismatch_aborts_before_switch_or_build() -> None:
    cfg = config()
    target = FakeTargetExecutor(current=f"releases/{SHA1}", checksum_mismatch=True)
    service = DeploymentService(cfg, git=FakeGit(SHA2), executor_factory=lambda _: target)

    with pytest.raises(DeploymentError, match="checksum mismatch"):
        await service.update(app_name="app-a", channel_id=111)

    assert target.current == f"releases/{SHA1}"
    assert not any(call[:2] == ("docker", "compose") for call in target.calls)


@pytest.mark.asyncio
async def test_extraction_failure_leaves_current_untouched() -> None:
    cfg = config()
    target = FakeTargetExecutor(current=f"releases/{SHA1}", extract_code=2)
    service = DeploymentService(cfg, git=FakeGit(SHA2), executor_factory=lambda _: target)

    with pytest.raises(DeploymentError, match="extract release failed"):
        await service.update(app_name="app-a", channel_id=111)

    assert target.current == f"releases/{SHA1}"
    assert not any(call[:2] == ("docker", "compose") for call in target.calls)


@pytest.mark.asyncio
async def test_build_failure_restores_previous_current_without_up() -> None:
    cfg = config()
    target = FakeTargetExecutor(current=f"releases/{SHA1}", build_codes=[1])
    service = DeploymentService(cfg, git=FakeGit(SHA2), executor_factory=lambda _: target)

    with pytest.raises(DeploymentError, match="build failed") as exc:
        await service.update(app_name="app-a", channel_id=111)

    assert target.current == f"releases/{SHA1}"
    assert b"build failed" in exc.value.transcript.render()
    assert not any("up" in call for call in target.calls if call[:2] == ("docker", "compose"))


@pytest.mark.asyncio
async def test_up_failure_restores_and_attempts_rollback_activation() -> None:
    cfg = config()
    target = FakeTargetExecutor(
        current=f"releases/{SHA1}",
        build_codes=[0, 0],
        up_codes=[1, 0],
    )
    service = DeploymentService(cfg, git=FakeGit(SHA2), executor_factory=lambda _: target)

    with pytest.raises(DeploymentError, match="rollback activation succeeded") as exc:
        await service.update(app_name="app-a", channel_id=111)

    assert target.current == f"releases/{SHA1}"
    compose_calls = [call for call in target.calls if call[:2] == ("docker", "compose")]
    assert sum("build" in call for call in compose_calls) == 2
    assert sum("up" in call for call in compose_calls) == 2
    assert b"rollback up" in exc.value.transcript.render()


@pytest.mark.asyncio
async def test_wrong_channel_rejected_before_git_or_target_access() -> None:
    cfg = config()
    git = FakeGit(SHA2)
    target = FakeTargetExecutor()
    service = DeploymentService(cfg, git=git, executor_factory=lambda _: target)

    with pytest.raises(ChannelAuthorizationError):
        await service.update(app_name="app-a", channel_id=999)

    assert git.fetch_calls == 0
    assert target.calls == []


@pytest.mark.asyncio
async def test_update_and_lifecycle_share_stack_lock_when_registry_is_shared() -> None:
    cfg = config()
    locks = StackLockRegistry()
    target = FakeTargetExecutor(current=f"releases/{SHA1}")
    service = DeploymentService(
        cfg,
        git=FakeGit(SHA2),
        executor_factory=lambda _: target,
        locks=locks,
    )
    compose = ComposeManager(cfg, executor_factory=lambda _: target, locks=locks)

    # Structural assertion: both services use the exact same registry, which
    # is the non-reentrant stack serialization boundary used at runtime.
    assert service.locks is locks
    assert compose.locks is locks
    assert await locks.get("bots") is await locks.get(resolve_app(cfg, "bots", "app-a").stack_name)
