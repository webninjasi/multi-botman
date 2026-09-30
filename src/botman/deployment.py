"""Staged source deployment from the central Git cache to one Compose service."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from .compose import ComposeManager, StackLockRegistry, executor_for_resolved_app
from .config import ConfigStore
from .executor import ExecResult, Executor
from .git import GitError, GitRepositoryManager
from .models import BotmanConfig
from .routing import ResolvedApp, authorize_app_channel

SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
DeploymentStatus = Literal["deployed", "noop"]


class DeploymentError(RuntimeError):
    def __init__(self, message: str, transcript: "DeploymentTranscript"):
        self.transcript = transcript
        super().__init__(message)


@dataclass(slots=True)
class DeploymentTranscript:
    lines: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.lines.append(text.rstrip("\n"))

    def command(self, label: str, result: ExecResult) -> None:
        self.note(f"[{label}] exit={result.returncode}")
        if result.stdout:
            self.note(result.stdout.rstrip("\n"))
        if result.stderr:
            self.note("stderr:")
            self.note(result.stderr.rstrip("\n"))

    def render(self) -> bytes:
        return ("\n".join(self.lines).rstrip() + "\n").encode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    status: DeploymentStatus
    app_name: str
    sha: str
    previous_sha: str | None
    transcript: bytes


ExecutorFactory = Callable[[ResolvedApp], Executor]


class DeploymentService:
    def __init__(
        self,
        config: BotmanConfig | ConfigStore,
        *,
        git: GitRepositoryManager | None = None,
        executor_factory: ExecutorFactory = executor_for_resolved_app,
        locks: StackLockRegistry | None = None,
    ):
        self.config_source = config
        initial = self._config()
        self._default_git = git is None
        self.git = git or GitRepositoryManager(initial)
        self.executor_factory = executor_factory
        self.locks = locks or StackLockRegistry()

    def _config(self) -> BotmanConfig:
        if isinstance(self.config_source, ConfigStore):
            return self.config_source.load_or_default()
        return self.config_source

    async def update(
        self,
        *,
        app_name: str,
        channel_id: str | int,
        progress: Callable[[str], object] | None = None,
    ) -> DeploymentResult:
        initial = authorize_app_channel(self._config(), app_name, channel_id)
        lock = await self.locks.get(initial.stack_name)
        transcript = DeploymentTranscript()

        async def note(message: str) -> None:
            transcript.note(message)
            if progress is not None:
                maybe = progress(message)
                if hasattr(maybe, "__await__"):
                    await maybe  # type: ignore[misc]

        async with lock:
            config = self._config()
            resolved = authorize_app_channel(config, app_name, channel_id)
            if resolved.stack_name != initial.stack_name:
                raise RuntimeError("app stack changed while waiting for deployment lock; retry")
            git = (
                GitRepositoryManager(config, runner=self.git.runner)
                if self._default_git
                else self.git
            )
            compose = ComposeManager(
                config, executor_factory=self.executor_factory, locks=self.locks
            )
            executor = self.executor_factory(resolved)
            archive_path: Path | None = None
            remote_archive: PurePosixPath | None = None
            staged: PurePosixPath | None = None
            try:
                await note(f"fetching {app_name}:{resolved.app.git.branch}")
                try:
                    revision = await git.fetch(resolved)
                except GitError as exc:
                    raise DeploymentError(f"Git fetch failed: {exc}", transcript) from exc
                sha = revision.sha
                await note(f"resolved commit {sha}")

                previous_target = await self._current_target(executor, resolved, config)
                previous_sha = self._sha_from_target(previous_target)
                if previous_target is not None and previous_sha is None:
                    raise DeploymentError(
                        "active current symlink has an unsafe or unrecognized target; "
                        "expected releases/<git-sha>",
                        transcript,
                    )
                if previous_sha == sha:
                    await note("target already runs this commit; no changes")
                    return DeploymentResult(
                        status="noop",
                        app_name=app_name,
                        sha=sha,
                        previous_sha=previous_sha,
                        transcript=transcript.render(),
                    )

                with tempfile.NamedTemporaryFile(
                    prefix=f"botman-{app_name}-{sha[:12]}-",
                    suffix=".tar",
                    delete=False,
                ) as handle:
                    archive_path = Path(handle.name)
                try:
                    try:
                        await git.archive(resolved, sha, archive_path)
                        self._validate_archive_paths(archive_path)
                    except GitError as exc:
                        raise DeploymentError(f"Git archive failed: {exc}", transcript) from exc
                    archive_hash = self._sha256_file(archive_path)
                    await note(f"archive ready sha256={archive_hash}")

                    app_root = config.app_release_root(resolved.stack_name, resolved.name)
                    releases = app_root / "releases"
                    token = uuid.uuid4().hex
                    remote_archive = app_root / f".botman-upload-{token}.tar"
                    staged = releases / f".{sha}.tmp-{token}"
                    release = releases / sha
                    current = app_root / "current"
                    temp_link = app_root / f".current-{token}"

                    await self._checked(
                        executor,
                        ("mkdir", "-p", "--", str(releases)),
                        transcript,
                        "mkdir release root",
                        timeout=30,
                    )
                    upload = getattr(executor, "upload", None)
                    if not callable(upload):
                        raise DeploymentError("target executor does not support archive upload", transcript)
                    await upload(archive_path, remote_archive)
                    await note("archive uploaded")

                    checksum = await executor.run(
                        ("sha256sum", "--", str(remote_archive)), timeout=60, check=False
                    )
                    transcript.command("target checksum", checksum)
                    if not checksum.ok:
                        raise DeploymentError("target checksum command failed", transcript)
                    actual_hash = checksum.stdout.strip().split(maxsplit=1)[0].lower()
                    if actual_hash != archive_hash:
                        raise DeploymentError(
                            f"archive checksum mismatch: expected {archive_hash}, got {actual_hash}",
                            transcript,
                        )
                    await note("archive checksum verified")

                    await executor.run(("rm", "-rf", "--", str(staged)), timeout=60, check=False)
                    await self._checked(
                        executor,
                        ("mkdir", "-p", "--", str(staged)),
                        transcript,
                        "create staging release",
                        timeout=30,
                    )
                    await self._checked(
                        executor,
                        (
                            "tar",
                            "-xf",
                            str(remote_archive),
                            "-C",
                            str(staged),
                            "--no-same-owner",
                            "--no-same-permissions",
                        ),
                        transcript,
                        "extract release",
                        timeout=120,
                    )

                    metadata = json.dumps(
                        {
                            "stack": resolved.stack_name,
                            "app": app_name,
                            "repo": resolved.app.git.repo_url,
                            "branch": resolved.app.git.branch,
                            "sha": sha,
                            "deployed_at": datetime.now(UTC).isoformat(),
                            "archive_sha256": archive_hash,
                        },
                        sort_keys=True,
                        indent=2,
                    ).encode()
                    write_bytes = getattr(executor, "write_bytes", None)
                    if not callable(write_bytes):
                        raise DeploymentError("target executor does not support metadata writes", transcript)
                    await write_bytes(staged / ".botman-release.json", metadata, mode=0o644, atomic=True)

                    # Any same-SHA release here is inactive because current SHA was
                    # checked above and stack mutations are serialized.
                    await executor.run(("rm", "-rf", "--", str(release)), timeout=60, check=False)
                    await self._checked(
                        executor,
                        ("mv", "--", str(staged), str(release)),
                        transcript,
                        "finalize release",
                        timeout=60,
                    )
                    staged = None
                    await self._switch_current(
                        executor,
                        current=current,
                        temp_link=temp_link,
                        target=f"releases/{sha}",
                        transcript=transcript,
                    )
                    await note("current release switched")

                    build = await executor.run(
                        (*compose.base_argv(resolved), "build", resolved.app.service),
                        timeout=900,
                        check=False,
                    )
                    transcript.command("compose build", build)
                    if not build.ok:
                        await self._restore_current(
                            executor, current, temp_link, previous_target, transcript
                        )
                        raise DeploymentError("Compose build failed; current release restored", transcript)

                    up = await executor.run(
                        (
                            *compose.base_argv(resolved),
                            "up",
                            "-d",
                            "--no-deps",
                            resolved.app.service,
                        ),
                        timeout=180,
                        check=False,
                    )
                    transcript.command("compose up", up)
                    if not up.ok:
                        await self._restore_current(
                            executor, current, temp_link, previous_target, transcript
                        )
                        rollback_ok = await self._rollback_activation(executor, resolved, transcript, compose)
                        message = "Compose activation failed; previous current restored"
                        message += "; rollback activation succeeded" if rollback_ok else "; rollback activation failed"
                        raise DeploymentError(message, transcript)

                    await note("deployment active")
                    await self._prune_releases(
                        executor, resolved, active_sha=sha, transcript=transcript, config=config
                    )
                    return DeploymentResult(
                        status="deployed",
                        app_name=app_name,
                        sha=sha,
                        previous_sha=previous_sha,
                        transcript=transcript.render(),
                    )
                finally:
                    if archive_path is not None:
                        archive_path.unlink(missing_ok=True)
            finally:
                if remote_archive is not None:
                    await executor.run(
                        ("rm", "-f", "--", str(remote_archive)), timeout=30, check=False
                    )
                if staged is not None:
                    await executor.run(("rm", "-rf", "--", str(staged)), timeout=60, check=False)

    async def _current_target(
        self, executor: Executor, resolved: ResolvedApp, config: BotmanConfig
    ) -> str | None:
        current = config.app_release_root(resolved.stack_name, resolved.name) / "current"
        result = await executor.run(("readlink", "--", str(current)), timeout=30, check=False)
        if result.ok:
            target = result.stdout.strip()
            return target or None
        return None

    @staticmethod
    def _sha_from_target(target: str | None) -> str | None:
        if not target:
            return None
        path = PurePosixPath(target)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "releases":
            return None
        candidate = path.parts[1].lower()
        return candidate if SHA_RE.fullmatch(candidate) else None

    @staticmethod
    def _resolve_archive_link(member_path: PurePosixPath, linkname: str) -> PurePosixPath:
        link = PurePosixPath(linkname)
        if not linkname or link.is_absolute():
            raise ValueError(f"unsafe archive symlink target: {linkname!r}")
        parts: list[str] = list(member_path.parent.parts)
        for part in link.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    raise ValueError(f"archive symlink escapes release root: {linkname!r}")
                parts.pop()
                continue
            parts.append(part)
        return PurePosixPath(*parts)

    @classmethod
    def _validate_archive_paths(cls, path: Path) -> None:
        try:
            with tarfile.open(path, "r:") as archive:
                for member in archive.getmembers():
                    member_path = PurePosixPath(member.name)
                    if (
                        not member.name
                        or member_path.is_absolute()
                        or ".." in member_path.parts
                        or member_path == PurePosixPath(".")
                    ):
                        raise ValueError(f"unsafe archive member: {member.name!r}")
                    if member.islnk():
                        # git archive does not need hard links. Reject them rather
                        # than relying on tar implementation-specific link handling.
                        raise ValueError(
                            f"hard links are not allowed in source archives: {member.name!r}"
                        )
                    if member.issym():
                        cls._resolve_archive_link(member_path, member.linkname)
                    if member.isdev() or member.isfifo():
                        raise ValueError(
                            f"special files are not allowed in source archives: {member.name!r}"
                        )
        except (tarfile.TarError, OSError, ValueError) as exc:
            raise GitError(f"generated source archive is unsafe or unreadable: {exc}") from exc

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    async def _checked(
        executor: Executor,
        argv: tuple[str, ...],
        transcript: DeploymentTranscript,
        label: str,
        *,
        timeout: float,
    ) -> ExecResult:
        result = await executor.run(argv, timeout=timeout, check=False)
        transcript.command(label, result)
        if not result.ok:
            raise DeploymentError(f"{label} failed", transcript)
        return result

    async def _switch_current(
        self,
        executor: Executor,
        *,
        current: PurePosixPath,
        temp_link: PurePosixPath,
        target: str,
        transcript: DeploymentTranscript,
    ) -> None:
        await executor.run(("rm", "-f", "--", str(temp_link)), timeout=30, check=False)
        await self._checked(
            executor,
            ("ln", "-s", "--", target, str(temp_link)),
            transcript,
            "create current symlink",
            timeout=30,
        )
        await self._checked(
            executor,
            ("mv", "-Tf", "--", str(temp_link), str(current)),
            transcript,
            "activate current symlink",
            timeout=30,
        )

    async def _restore_current(
        self,
        executor: Executor,
        current: PurePosixPath,
        temp_link: PurePosixPath,
        previous_target: str | None,
        transcript: DeploymentTranscript,
    ) -> None:
        if previous_target is None:
            result = await executor.run(("rm", "-f", "--", str(current)), timeout=30, check=False)
            transcript.command("restore empty current", result)
            return
        await self._switch_current(
            executor,
            current=current,
            temp_link=temp_link,
            target=previous_target,
            transcript=transcript,
        )
        transcript.note(f"restored current -> {previous_target}")

    async def _rollback_activation(
        self,
        executor: Executor,
        resolved: ResolvedApp,
        transcript: DeploymentTranscript,
        compose: ComposeManager,
    ) -> bool:
        build = await executor.run(
            (*compose.base_argv(resolved), "build", resolved.app.service),
            timeout=900,
            check=False,
        )
        transcript.command("rollback build", build)
        if not build.ok:
            return False
        up = await executor.run(
            (*compose.base_argv(resolved), "up", "-d", "--no-deps", resolved.app.service),
            timeout=180,
            check=False,
        )
        transcript.command("rollback up", up)
        return up.ok

    async def _prune_releases(
        self,
        executor: Executor,
        resolved: ResolvedApp,
        *,
        active_sha: str,
        transcript: DeploymentTranscript,
        config: BotmanConfig,
    ) -> None:
        releases = config.app_release_root(resolved.stack_name, resolved.name) / "releases"
        result = await executor.run(
            (
                "find",
                str(releases),
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-type",
                "d",
                "-printf",
                "%T@ %f\\n",
            ),
            timeout=30,
            check=False,
        )
        transcript.command("list releases", result)
        if not result.ok:
            transcript.note("release pruning skipped: unable to list releases")
            return
        candidates: list[tuple[float, str]] = []
        for line in result.stdout.splitlines():
            try:
                stamp_raw, name = line.split(" ", 1)
                stamp = float(stamp_raw)
            except ValueError:
                continue
            if SHA_RE.fullmatch(name):
                candidates.append((stamp, name))
        candidates.sort(reverse=True)
        keep_count = config.settings.release_keep_count
        kept: set[str] = {active_sha}
        for _, name in candidates:
            if len(kept) >= keep_count:
                break
            kept.add(name)
        for _, name in candidates:
            if name in kept:
                continue
            removed = await executor.run(
                ("rm", "-rf", "--", str(releases / name)), timeout=60, check=False
            )
            transcript.command(f"prune {name}", removed)
