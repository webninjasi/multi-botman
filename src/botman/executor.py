"""Argv-safe local and SSH command execution with strict SSH verification."""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, runtime_checkable

try:  # Kept optional at import time so config-only tools/tests remain usable.
    import asyncssh  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only when dependency omitted.
    asyncssh = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class ExecResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> "ExecResult":
        if not self.ok:
            raise CommandError(self)
        return self


@dataclass(frozen=True, slots=True)
class StreamLine:
    stream: Literal["stdout", "stderr"]
    text: str


class CommandError(RuntimeError):
    def __init__(self, result: ExecResult):
        self.result = result
        rendered = shlex.join(result.argv)
        super().__init__(f"command failed with exit {result.returncode}: {rendered}")


class CommandTimeout(TimeoutError):
    def __init__(self, argv: Sequence[str], timeout: float):
        self.argv = tuple(argv)
        self.timeout = timeout
        super().__init__(f"command timed out after {timeout}s: {shlex.join(self.argv)}")


def _normalize_argv(argv: Sequence[str | os.PathLike[str]]) -> tuple[str, ...]:
    normalized = tuple(os.fspath(arg) for arg in argv)
    if not normalized:
        raise ValueError("argv must not be empty")
    if any((not arg or "\x00" in arg) for arg in normalized):
        raise ValueError("argv entries must be non-empty and contain no NUL bytes")
    return normalized


@runtime_checkable
class Executor(Protocol):
    async def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult: ...

    def stream(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> AsyncIterator[StreamLine]: ...


class LocalExecutor:
    async def read_bytes(self, path: str | Path | PurePosixPath) -> bytes:
        return await asyncio.to_thread(Path(path).read_bytes)

    async def write_bytes(
        self,
        path: str | Path | PurePosixPath,
        data: bytes,
        *,
        mode: int = 0o600,
        atomic: bool = True,
    ) -> None:
        target = Path(path)

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not atomic:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                os.chmod(target, mode)
                return

            temp = target.with_name(f".{target.name}.botman-{uuid.uuid4().hex}.tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
                os.chmod(target, mode)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

        await asyncio.to_thread(write)

    async def upload(self, local_path: str | Path, remote_path: str | Path | PurePosixPath) -> None:
        data = await asyncio.to_thread(Path(local_path).read_bytes)
        await self.write_bytes(remote_path, data, mode=0o600, atomic=True)

    async def download(self, remote_path: str | Path | PurePosixPath, local_path: str | Path) -> None:
        data = await self.read_bytes(remote_path)
        target = Path(local_path)
        await asyncio.to_thread(target.write_bytes, data)

    async def write_privileged_bytes(
        self,
        path: str | Path | PurePosixPath,
        data: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        """Atomically replace a privileged local path through checked sudo commands."""
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("privileged target path must be absolute")
        token = uuid.uuid4().hex
        upload = Path("/tmp") / f"botman-upload-{token}"
        stage = target.with_name(f".{target.name}.botman-{token}.tmp")
        await self.write_bytes(upload, data, mode=0o600, atomic=False)
        try:
            await self.run(
                ["sudo", "install", "-m", f"{mode:04o}", "--", str(upload), str(stage)],
                check=True,
            )
            await self.run(["sudo", "chmod", f"{mode:04o}", "--", str(stage)], check=True)
            await self.run(["sudo", "mv", "--", str(stage), str(target)], check=True)
        finally:
            await self.run(["rm", "-f", "--", str(upload)], check=False)
            await self.run(["sudo", "rm", "-f", "--", str(stage)], check=False)

    async def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult:
        args = _normalize_argv(argv)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CommandTimeout(args, timeout if timeout is not None else 0.0) from exc
        result = ExecResult(
            argv=args,
            returncode=proc.returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
        )
        return result.check() if check else result

    async def stream(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> AsyncIterator[StreamLine]:
        args = _normalize_argv(argv)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        queue: asyncio.Queue[StreamLine | tuple[str, None]] = asyncio.Queue(maxsize=100)

        async def pump(name: Literal["stdout", "stderr"], reader: asyncio.StreamReader) -> None:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                await queue.put(StreamLine(name, raw.decode("utf-8", errors="replace").rstrip("\n")))
            await queue.put((name, None))

        tasks = [
            asyncio.create_task(pump("stdout", proc.stdout)),
            asyncio.create_task(pump("stderr", proc.stderr)),
        ]
        finished = 0
        try:
            async with asyncio.timeout(timeout):
                while finished < 2:
                    item = await queue.get()
                    if isinstance(item, tuple):
                        finished += 1
                    else:
                        yield item
                await proc.wait()
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CommandTimeout(args, timeout if timeout is not None else 0.0) from exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if check and proc.returncode:
            raise CommandError(ExecResult(args, proc.returncode, "", ""))


class SSHExecutor:
    """Transient SSH/SFTP executor.

    Host verification is always enabled. If ``known_hosts`` is omitted,
    AsyncSSH's normal OpenSSH known_hosts lookup is used. This class never
    passes ``known_hosts=None`` because that disables verification.
    """

    def __init__(
        self,
        *,
        host: str,
        user: str,
        key: str | Path,
        port: int = 22,
        known_hosts: str | Path | None = None,
        connect_timeout: float = 15.0,
    ):
        if not host:
            raise ValueError("host is required")
        if not user:
            raise ValueError("user is required")
        if not 1 <= port <= 65_535:
            raise ValueError("port must be 1..65535")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        self.host = host
        self.user = user
        self.key = str(key)
        self.port = port
        self.known_hosts = str(known_hosts) if known_hosts is not None else None
        self.connect_timeout = connect_timeout

    def _require_asyncssh(self):
        if asyncssh is None:
            raise RuntimeError("asyncssh is required for SSHExecutor; install the project dependencies")
        return asyncssh

    def _connect_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "client_keys": [self.key],
            "connect_timeout": self.connect_timeout,
            "preferred_auth": ["publickey"],
            "public_key_auth": True,
            "password_auth": False,
            "kbdint_auth": False,
            "host_based_auth": False,
            "gss_auth": False,
            "gss_kex": False,
            "agent_path": None,
        }
        if self.known_hosts is not None:
            kwargs["known_hosts"] = self.known_hosts
        return kwargs

    async def _connect(self):
        library = self._require_asyncssh()
        # AsyncSSH's connect_timeout itself includes TCP + handshake/auth, and
        # wait_for is a second outer bound guarding unexpected library stalls.
        return await asyncio.wait_for(
            library.connect(**self._connect_kwargs()), timeout=self.connect_timeout + 1.0
        )

    async def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> ExecResult:
        args = _normalize_argv(argv)
        remote_command = shlex.join(args)
        conn = await self._connect()
        try:
            try:
                remote_result = await asyncio.wait_for(conn.run(remote_command, check=False), timeout=timeout)
            except TimeoutError as exc:
                raise CommandTimeout(args, timeout if timeout is not None else 0.0) from exc
            result = ExecResult(
                argv=args,
                returncode=int(remote_result.exit_status),
                stdout=str(remote_result.stdout or ""),
                stderr=str(remote_result.stderr or ""),
            )
            return result.check() if check else result
        finally:
            conn.close()
            await conn.wait_closed()

    async def stream(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> AsyncIterator[StreamLine]:
        args = _normalize_argv(argv)
        conn = await self._connect()
        process = None
        queue: asyncio.Queue[StreamLine | tuple[str, None]] = asyncio.Queue(maxsize=100)
        tasks: list[asyncio.Task[None]] = []
        try:
            process = await conn.create_process(shlex.join(args))

            async def pump(name: Literal["stdout", "stderr"], reader) -> None:
                async for raw in reader:
                    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                    await queue.put(StreamLine(name, text.rstrip("\n")))
                await queue.put((name, None))

            tasks = [
                asyncio.create_task(pump("stdout", process.stdout)),
                asyncio.create_task(pump("stderr", process.stderr)),
            ]
            finished = 0
            try:
                async with asyncio.timeout(timeout):
                    while finished < 2:
                        item = await queue.get()
                        if isinstance(item, tuple):
                            finished += 1
                        else:
                            yield item
                    await process.wait()
            except TimeoutError as exc:
                process.terminate()
                raise CommandTimeout(args, timeout if timeout is not None else 0.0) from exc
            if check and process.exit_status:
                raise CommandError(ExecResult(args, int(process.exit_status), "", ""))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            conn.close()
            await conn.wait_closed()

    async def read_bytes(self, remote_path: str | PurePosixPath) -> bytes:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(str(remote_path), "rb") as handle:
                    return await handle.read()
        finally:
            conn.close()
            await conn.wait_closed()

    async def write_bytes(
        self,
        remote_path: str | PurePosixPath,
        data: bytes,
        *,
        mode: int = 0o600,
        atomic: bool = True,
    ) -> None:
        target = PurePosixPath(str(remote_path))
        temp = target.with_name(f".{target.name}.botman-{uuid.uuid4().hex}.tmp")
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                path = temp if atomic else target
                try:
                    async with sftp.open(str(path), "wb") as handle:
                        await handle.write(data)
                    await sftp.chmod(str(path), mode)
                    if atomic:
                        await sftp.posix_rename(str(path), str(target))
                        await sftp.chmod(str(target), mode)
                except Exception:
                    if atomic:
                        try:
                            await sftp.remove(str(temp))
                        except Exception:
                            pass
                    raise
        finally:
            conn.close()
            await conn.wait_closed()

    async def upload(self, local_path: str | Path, remote_path: str | PurePosixPath) -> None:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.put(str(local_path), str(remote_path))
        finally:
            conn.close()
            await conn.wait_closed()

    async def download(self, remote_path: str | PurePosixPath, local_path: str | Path) -> None:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.get(str(remote_path), str(local_path))
        finally:
            conn.close()
            await conn.wait_closed()

    async def write_privileged_bytes(
        self,
        remote_path: str | PurePosixPath,
        data: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        """Write a root-owned path via a unique upload/stage + checked sudo steps.

        The final ``mv`` is within the destination filesystem, allowing atomic
        replacement where the filesystem supports normal rename semantics.
        """

        target = PurePosixPath(str(remote_path))
        if not target.is_absolute():
            raise ValueError("privileged target path must be absolute")
        token = uuid.uuid4().hex
        upload = PurePosixPath("/tmp") / f"botman-upload-{token}"
        stage = target.with_name(f".{target.name}.botman-{token}.tmp")
        await self.write_bytes(upload, data, mode=0o600, atomic=False)
        try:
            await self.run(
                ["sudo", "install", "-m", f"{mode:04o}", "--", str(upload), str(stage)],
                check=True,
            )
            # Explicit chmod is deliberately checked too; provisioning must
            # never claim success if restrictive permissions could not be set.
            await self.run(["sudo", "chmod", f"{mode:04o}", "--", str(stage)], check=True)
            await self.run(["sudo", "mv", "--", str(stage), str(target)], check=True)
        finally:
            await self.run(["rm", "-f", "--", str(upload)], check=False)
            await self.run(["sudo", "rm", "-f", "--", str(stage)], check=False)
