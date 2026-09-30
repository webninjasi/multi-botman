from __future__ import annotations

from pathlib import Path

import pytest

from botman.executor import ExecResult
from botman.git import GitError, GitRepositoryManager, git_ssh_environment
from botman.models import BotmanConfig


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

    revision = await manager.fetch("app-a")

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
    cfg.apps["app-a"].git.deploy_key_path = None
    manager = GitRepositoryManager(cfg, runner=RecordingGitRunner())
    with pytest.raises(GitError, match="no deploy key"):
        await manager.fetch("app-a")


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
    private, public = await manager.generate("app-a")

    assert private == tmp_path / "keys" / "app-a"
    assert public == "ssh-ed25519 AAAATEST botman"
    assert os.stat(private).st_mode & 0o777 == 0o600
    assert os.stat(Path(f"{private}.pub")).st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_git_admin_setup_persists_generated_private_key_path(tmp_path: Path) -> None:
    from botman.config import ConfigStore
    from botman.git import GitAdminService

    cfg = config(tmp_path)
    cfg.apps["app-a"].git.deploy_key_path = None
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(cfg)

    public = await GitAdminService(store, runner=KeyCreatingRunner()).setup("app-a")

    loaded = store.load()
    assert public.startswith("ssh-ed25519 ")
    assert loaded.apps["app-a"].git.deploy_key_path == tmp_path / "keys" / "app-a"
