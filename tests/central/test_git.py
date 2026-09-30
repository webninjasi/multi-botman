from __future__ import annotations

from pathlib import Path

import pytest

from botman.executor import ExecResult
from botman.git import GitError, GitRepositoryManager, git_ssh_environment
from botman.models import BotmanConfig
from botman.routing import resolve_app


def config(tmp_path: Path) -> BotmanConfig:
    key = tmp_path / "deploy-key"
    known = tmp_path / "known_hosts"
    return BotmanConfig.model_validate(
        {
            "settings": {
                "repo_cache_root": str(tmp_path / "repos"),
                "git_known_hosts": str(known),
                "deploy_key_root": str(tmp_path / "keys"),
            },
            "servers": {"local": {"type": "local"}},
            "stacks": {
                "bots": {
                    "server": "local",
                    "channel_id": "1",
                    "project_name": "bots",
                }
            },
            "apps": {
                "app-a": {
                    "stack": "bots",
                    "service": "app-a",
                    "log_identifier": "bots-app-a",
                    "git": {
                        "repo_url": "git@example.com:owner/app-a.git",
                        "branch": "feature/test",
                        "deploy_key_path": str(key),
                    },
                }
            },
        }
    )


class RecordingGitRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    async def run(self, argv, *, env, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        self.calls.append((args, env.copy()))
        if "rev-parse" in args:
            return ExecResult(args, 0, "a" * 40 + "\n", "")
        if args[-4:-2] == ("remote", "set-url") or ("remote" in args and "set-url" in args):
            return ExecResult(args, 0, "", "")
        return ExecResult(args, 0, "", "")


def test_git_ssh_environment_forces_strict_host_verification(tmp_path: Path) -> None:
    env = git_ssh_environment(key_path=tmp_path / "key with space", known_hosts=tmp_path / "known hosts")
    command = env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=yes" in command
    assert "IdentitiesOnly=yes" in command
    assert "UserKnownHostsFile=" in command
    assert "BatchMode=yes" in command
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "StrictHostKeyChecking=no" not in command


@pytest.mark.asyncio
async def test_fetch_uses_exact_branch_refspec_and_resolves_commit(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    runner = RecordingGitRunner()
    manager = GitRepositoryManager(cfg, runner=runner)

    revision = await manager.fetch(resolve_app(cfg, "bots", "app-a"))

    assert revision.sha == "a" * 40
    fetch = next(args for args, _ in runner.calls if "fetch" in args)
    assert fetch[-1] == "+refs/heads/feature/test:refs/remotes/origin/feature/test"
    resolve = next(args for args, _ in runner.calls if "rev-parse" in args)
    assert resolve[-1] == "refs/remotes/origin/feature/test^{commit}"
    for _, env in runner.calls:
        assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]


@pytest.mark.asyncio
async def test_fetch_requires_deploy_key(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.stacks["bots"].apps["app-a"].git.deploy_key_path = None
    manager = GitRepositoryManager(cfg, runner=RecordingGitRunner())
    with pytest.raises(GitError, match="no deploy key"):
        await manager.fetch(resolve_app(cfg, "bots", "app-a"))


class KeyCreatingRunner:
    async def run(self, argv, *, env, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        key_path = Path(args[args.index("-f") + 1])
        key_path.write_text("PRIVATE", encoding="utf-8")
        Path(f"{key_path}.pub").write_text("ssh-ed25519 AAAATEST botman\n", encoding="utf-8")
        return ExecResult(args, 0, "", "")


@pytest.mark.asyncio
async def test_deploy_key_manager_generates_restrictive_keypair(tmp_path: Path) -> None:
    import os

    from botman.git import DeployKeyManager

    cfg = config(tmp_path)
    manager = DeployKeyManager(cfg, runner=KeyCreatingRunner())
    private, public = await manager.generate("bots", "app-a")

    assert private == tmp_path / "keys" / "bots" / "app-a"
    assert public == "ssh-ed25519 AAAATEST botman"
    assert os.stat(private).st_mode & 0o777 == 0o600
    assert os.stat(Path(f"{private}.pub")).st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_git_admin_setup_persists_generated_private_key_path(tmp_path: Path) -> None:
    from botman.config import ConfigStore
    from botman.git import GitAdminService

    cfg = config(tmp_path)
    cfg.stacks["bots"].apps["app-a"].git.deploy_key_path = None
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(cfg)

    public = await GitAdminService(store, runner=KeyCreatingRunner()).setup("app-a", channel_id=1)

    loaded = store.load()
    assert public.startswith("ssh-ed25519 ")
    assert loaded.stacks["bots"].apps["app-a"].git.deploy_key_path == tmp_path / "keys" / "bots" / "app-a"


def test_git_cache_and_key_paths_are_stack_namespaced(tmp_path: Path) -> None:
    from botman.git import DeployKeyManager

    cfg = BotmanConfig.model_validate(
        {
            "settings": {
                "repo_cache_root": str(tmp_path / "repos"),
                "git_known_hosts": str(tmp_path / "known_hosts"),
                "deploy_key_root": str(tmp_path / "keys"),
            },
            "servers": {"local": {"type": "local"}},
            "stacks": {
                "one": {
                    "server": "local",
                    "channel_id": "1",
                    "project_name": "one",
                    "apps": {
                        "app": {
                            "service": "app",
                            "log_identifier": "one-app",
                            "git": {"repo_url": "git@example.com:one/app.git"},
                        }
                    },
                },
                "two": {
                    "server": "local",
                    "channel_id": "2",
                    "project_name": "two",
                    "apps": {
                        "app": {
                            "service": "app",
                            "log_identifier": "two-app",
                            "git": {"repo_url": "git@example.com:two/app.git"},
                        }
                    },
                },
            },
        }
    )
    repos = GitRepositoryManager(cfg, runner=RecordingGitRunner())
    keys = DeployKeyManager(cfg, runner=KeyCreatingRunner())

    one = resolve_app(cfg, "one", "app")
    two = resolve_app(cfg, "two", "app")
    assert repos.repo_path(one) == tmp_path / "repos" / "one" / "app.git"
    assert repos.repo_path(two) == tmp_path / "repos" / "two" / "app.git"
    assert repos.repo_path(one) != repos.repo_path(two)
    assert keys.default_private_path("one", "app") == tmp_path / "keys" / "one" / "app"
    assert keys.default_private_path("two", "app") == tmp_path / "keys" / "two" / "app"

class FailingKeyRunner:
    async def run(self, argv, *, env, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        key_path = Path(args[args.index("-f") + 1])
        key_path.write_text("PARTIAL", encoding="utf-8")
        Path(f"{key_path}.pub").write_text("PARTIAL-PUB", encoding="utf-8")
        return ExecResult(args, 1, "", "keygen failed")


@pytest.mark.asyncio
async def test_deploy_key_rotation_failure_preserves_existing_pair(tmp_path: Path) -> None:
    from botman.git import DeployKeyManager

    cfg = config(tmp_path)
    manager = DeployKeyManager(cfg, runner=FailingKeyRunner())
    private = manager.default_private_path("bots", "app-a")
    public = Path(f"{private}.pub")
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("OLD-PRIVATE", encoding="utf-8")
    public.write_text("OLD-PUBLIC", encoding="utf-8")

    with pytest.raises(GitError, match="keygen failed"):
        await manager.generate("bots", "app-a", replace=True)

    assert private.read_text(encoding="utf-8") == "OLD-PRIVATE"
    assert public.read_text(encoding="utf-8") == "OLD-PUBLIC"
    assert not list(private.parent.glob(f".{private.name}.botman-*.tmp*"))
    assert not list(private.parent.glob(f".{private.name}.botman-*.bak*"))


@pytest.mark.asyncio
async def test_deploy_key_rotation_replaces_pair_only_after_staging(tmp_path: Path) -> None:
    from botman.git import DeployKeyManager

    cfg = config(tmp_path)
    manager = DeployKeyManager(cfg, runner=KeyCreatingRunner())
    private = manager.default_private_path("bots", "app-a")
    public = Path(f"{private}.pub")
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("OLD-PRIVATE", encoding="utf-8")
    public.write_text("OLD-PUBLIC", encoding="utf-8")

    returned_private, returned_public = await manager.generate("bots", "app-a", replace=True)

    assert returned_private == private
    assert returned_public == "ssh-ed25519 AAAATEST botman"
    assert private.read_text(encoding="utf-8") == "PRIVATE"
    assert public.read_text(encoding="utf-8").startswith("ssh-ed25519 AAAATEST")

@pytest.mark.asyncio
async def test_git_admin_setup_waits_for_shared_stack_lock(tmp_path: Path) -> None:
    import asyncio

    from botman.compose import StackLockRegistry
    from botman.config import ConfigStore
    from botman.git import GitAdminService

    cfg = config(tmp_path)
    cfg.stacks["bots"].apps["app-a"].git.deploy_key_path = None
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(cfg)
    locks = StackLockRegistry()
    service = GitAdminService(store, runner=KeyCreatingRunner(), locks=locks)

    lock = await locks.get("bots")
    await lock.acquire()
    task = asyncio.create_task(service.setup("app-a", channel_id=1))
    await asyncio.sleep(0)
    assert not task.done()
    assert store.load().stacks["bots"].apps["app-a"].git.deploy_key_path is None

    lock.release()
    await task
    assert (
        store.load().stacks["bots"].apps["app-a"].git.deploy_key_path
        == tmp_path / "keys" / "bots" / "app-a"
    )
