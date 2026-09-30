"""Central-only Git cache and deploy-key management."""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .compose import StackLockRegistry
from .executor import CommandError, CommandTimeout, ExecResult
from .models import AppConfig, BotmanConfig
from .routing import ResolvedApp, authorize_app_channel


class GitError(RuntimeError):
    """Git cache/key operation failed."""


class GitRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        env: dict[str, str],
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult: ...


class LocalGitRunner:
    """Argv-safe local subprocess runner with an explicit environment."""

    async def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        env: dict[str, str],
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult:
        args = tuple(os.fspath(arg) for arg in argv)
        if not args or any(not arg or "\x00" in arg for arg in args):
            raise ValueError("invalid Git argv")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CommandTimeout(args, timeout if timeout is not None else 0.0) from exc
        result = ExecResult(
            args,
            proc.returncode,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )
        return result.check() if check else result


def git_ssh_environment(*, key_path: Path, known_hosts: Path) -> dict[str, str]:
    """Return a controlled Git environment with strict SSH host verification."""

    command = shlex.join(
        [
            "ssh",
            "-i",
            str(key_path),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
        ]
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/var/lib/botman"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": command,
    }
    return env


@dataclass(frozen=True, slots=True)
class GitRevision:
    sha: str
    repo_path: Path


class GitRepositoryManager:
    def __init__(self, config: BotmanConfig, *, runner: GitRunner | None = None):
        self.config = config
        self.runner = runner or LocalGitRunner()

    def repo_path(self, resolved: ResolvedApp) -> Path:
        return self.config.settings.repo_cache_root / resolved.stack_name / f"{resolved.name}.git"

    def _app_env(self, app: AppConfig) -> dict[str, str]:
        key = app.git.deploy_key_path
        if key is None:
            raise GitError("app has no deploy key configured; run Git setup first")
        return git_ssh_environment(
            key_path=key,
            known_hosts=self.config.settings.git_known_hosts,
        )

    async def fetch(self, resolved: ResolvedApp) -> GitRevision:
        app = resolved.app
        repo = self.repo_path(resolved)
        repo.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = self._app_env(app)

        if not repo.exists():
            result = await self.runner.run(
                ("git", "init", "--bare", "--", str(repo)),
                env=env, timeout=30, check=False,
            )
            if not result.ok:
                raise GitError(result.stderr.strip() or "git init --bare failed")

        set_url = await self.runner.run(
            ("git", "--git-dir", str(repo), "remote", "set-url", "origin", app.git.repo_url),
            env=env, timeout=30, check=False,
        )
        if not set_url.ok:
            add = await self.runner.run(
                ("git", "--git-dir", str(repo), "remote", "add", "origin", app.git.repo_url),
                env=env, timeout=30, check=False,
            )
            if not add.ok:
                raise GitError(add.stderr.strip() or "unable to configure Git origin")

        remote_ref = f"refs/remotes/origin/{app.git.branch}"
        refspec = f"+refs/heads/{app.git.branch}:{remote_ref}"
        fetched = await self.runner.run(
            ("git", "--git-dir", str(repo), "fetch", "--prune", "origin", refspec),
            env=env, timeout=180, check=False,
        )
        if not fetched.ok:
            raise GitError(fetched.stderr.strip() or "git fetch failed")

        resolved_rev = await self.runner.run(
            ("git", "--git-dir", str(repo), "rev-parse", "--verify", f"{remote_ref}^{{commit}}"),
            env=env, timeout=30, check=False,
        )
        if not resolved_rev.ok:
            raise GitError(resolved_rev.stderr.strip() or "unable to resolve fetched branch")
        sha = resolved_rev.stdout.strip().lower()
        if len(sha) not in {40, 64} or any(ch not in "0123456789abcdef" for ch in sha):
            raise GitError(f"Git returned an invalid commit ID: {sha!r}")
        return GitRevision(sha=sha, repo_path=repo)

    async def archive(self, resolved: ResolvedApp, sha: str, destination: Path) -> None:
        app = resolved.app
        repo = self.repo_path(resolved)
        env = self._app_env(app)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = await self.runner.run(
            ("git", "--git-dir", str(repo), "archive", "--format=tar", "-o", str(destination), sha),
            env=env, timeout=120, check=False,
        )
        if not result.ok:
            raise GitError(result.stderr.strip() or "git archive failed")


class DeployKeyManager:
    """Generate one Ed25519 read-only deploy keypair path per stack/app."""

    def __init__(self, config: BotmanConfig, *, runner: GitRunner | None = None):
        self.config = config
        self.runner = runner or LocalGitRunner()

    def default_private_path(self, stack_name: str, app_name: str) -> Path:
        if stack_name not in self.config.stacks or app_name not in self.config.stacks[stack_name].apps:
            raise KeyError(f"unknown app {app_name!r} in stack {stack_name!r}")
        return self.config.settings.deploy_key_root / stack_name / app_name

    async def generate(
        self, stack_name: str, app_name: str, *, replace: bool = False
    ) -> tuple[Path, str]:
        private = self.default_private_path(stack_name, app_name)
        public = Path(f"{private}.pub")
        private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (private.exists() or public.exists()) and not replace:
            raise GitError(f"deploy key already exists for {stack_name}/{app_name}")

        token = uuid.uuid4().hex
        staged_private = private.with_name(f".{private.name}.botman-{token}.tmp")
        staged_public = Path(f"{staged_private}.pub")
        backup_private = private.with_name(f".{private.name}.botman-{token}.bak")
        backup_public = Path(f"{backup_private}.pub")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/var/lib/botman"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        try:
            result = await self.runner.run(
                (
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    f"botman deploy {stack_name}/{app_name}",
                    "-f",
                    str(staged_private),
                ),
                env=env,
                timeout=30,
                check=False,
            )
            if not result.ok:
                raise GitError(result.stderr.strip() or "ssh-keygen failed")
            os.chmod(staged_private, 0o600)
            os.chmod(staged_public, 0o644)
            try:
                public_text = staged_public.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise GitError(f"generated public key could not be read: {exc}") from exc
            if not public_text:
                raise GitError("generated public key is empty")

            old_private = private.exists()
            old_public = public.exists()
            try:
                if old_private:
                    os.replace(private, backup_private)
                if old_public:
                    os.replace(public, backup_public)
                os.replace(staged_private, private)
                os.replace(staged_public, public)
                os.chmod(private, 0o600)
                os.chmod(public, 0o644)
            except Exception as exc:
                # Restore the prior pair if activation of the staged pair fails.
                private.unlink(missing_ok=True)
                public.unlink(missing_ok=True)
                if backup_private.exists():
                    os.replace(backup_private, private)
                if backup_public.exists():
                    os.replace(backup_public, public)
                raise GitError(f"failed to activate generated deploy key: {exc}") from exc
            else:
                backup_private.unlink(missing_ok=True)
                backup_public.unlink(missing_ok=True)
            return private, public_text
        finally:
            staged_private.unlink(missing_ok=True)
            staged_public.unlink(missing_ok=True)
            # Backups are removed on successful activation. If a filesystem
            # error prevents restoration, leave any backup in place for manual
            # recovery rather than deleting the last known-good private key.


class GitAdminService:
    """Config-aware deploy key setup/rotation scoped by Discord stack channel."""

    def __init__(
        self,
        store,
        *,
        runner: GitRunner | None = None,
        locks: StackLockRegistry | None = None,
    ):
        self.store = store
        self.runner = runner
        self.locks = locks or StackLockRegistry()

    async def setup(
        self, app_name: str, *, channel_id: str | int, replace: bool = False
    ) -> str:
        initial = authorize_app_channel(self.store.load_or_default(), app_name, channel_id)
        lock = await self.locks.get(initial.stack_name)

        async def mutate(config: BotmanConfig) -> str:
            resolved = authorize_app_channel(config, app_name, channel_id)
            if resolved.stack_name != initial.stack_name:
                raise RuntimeError("app stack changed while configuring Git; retry the command")
            manager = DeployKeyManager(config, runner=self.runner)
            private, public = await manager.generate(
                resolved.stack_name, resolved.name, replace=replace
            )
            resolved.app.git.deploy_key_path = private
            return public

        async with lock:
            _, public = await self.store.mutate(mutate)
        return public
