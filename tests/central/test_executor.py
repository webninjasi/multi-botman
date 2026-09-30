from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import botman.executor as executor_module
from botman.executor import CommandError, CommandTimeout, ExecResult, LocalExecutor, SSHExecutor


@pytest.mark.asyncio
async def test_local_argv_preserves_spaces_and_metacharacters_without_shell() -> None:
    marker = "value with spaces;$(echo injected)&*"
    result = await LocalExecutor().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", marker], check=True
    )
    assert result.stdout.strip() == marker


@pytest.mark.asyncio
async def test_local_timeout_kills_child() -> None:
    with pytest.raises(CommandTimeout):
        await LocalExecutor().run(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05
        )


@pytest.mark.asyncio
async def test_local_streams_stdout_and_stderr() -> None:
    lines = []
    async for line in LocalExecutor().stream(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        check=True,
    ):
        lines.append((line.stream, line.text))
    assert ("stdout", "out") in lines
    assert ("stderr", "err") in lines


class FakeConnection:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.closed = False

    async def run(self, command: str, check: bool = False):
        self.commands.append(command)
        return SimpleNamespace(exit_status=0, stdout="ok\n", stderr="")

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ssh_strict_host_verification_timeout_and_quoting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    conn = FakeConnection()

    async def fake_connect(**kwargs):
        calls.append(kwargs)
        return conn

    monkeypatch.setattr(executor_module, "asyncssh", SimpleNamespace(connect=fake_connect))
    ssh = SSHExecutor(
        host="example.test",
        port=2222,
        user="botmgr",
        key="/home/botman/.ssh/server-test",
        known_hosts="/etc/botman/known_hosts",
        connect_timeout=7.0,
    )
    argv = ["printf", "%s", "space and ; shell $(tokens)"]
    result = await ssh.run(argv, check=True)

    assert result.stdout == "ok\n"
    assert calls[0]["known_hosts"] == "/etc/botman/known_hosts"
    assert calls[0]["known_hosts"] is not None
    assert calls[0]["connect_timeout"] == 7.0
    assert calls[0]["client_keys"] == ["/home/botman/.ssh/server-test"]
    assert calls[0]["public_key_auth"] is True
    assert calls[0]["password_auth"] is False
    assert calls[0]["kbdint_auth"] is False
    assert calls[0]["host_based_auth"] is False
    assert calls[0]["gss_auth"] is False
    assert calls[0]["gss_kex"] is False
    assert calls[0]["agent_path"] is None
    assert conn.commands == [shlex.join(argv)]


@pytest.mark.asyncio
async def test_ssh_default_known_hosts_is_not_explicitly_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    conn = FakeConnection()

    async def fake_connect(**kwargs):
        calls.append(kwargs)
        return conn

    monkeypatch.setattr(executor_module, "asyncssh", SimpleNamespace(connect=fake_connect))
    ssh = SSHExecutor(host="example.test", user="botmgr", key="/tmp/key")
    await ssh.run(["true"])
    assert "known_hosts" not in calls[0]
    assert calls[0]["connect_timeout"] == 15.0


@pytest.mark.asyncio
async def test_connect_timeout_wraps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hanging_connect(**kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(executor_module, "asyncssh", SimpleNamespace(connect=hanging_connect))
    ssh = SSHExecutor(
        host="example.test", user="botmgr", key="/tmp/key", connect_timeout=0.01
    )
    # Constructor intentionally enforces positive only; outer wait adds 1s,
    # so patch wait_for to prove the bound includes connect rather than sleeping.
    seen = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        seen.append(timeout)
        if len(seen) == 1:
            awaitable.close()
            raise TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(executor_module.asyncio, "wait_for", recording_wait_for)
    with pytest.raises(TimeoutError):
        await ssh._connect()
    assert seen == [pytest.approx(1.01)]


@pytest.mark.asyncio
async def test_privileged_write_uses_unique_paths_and_checks_mode_and_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh = SSHExecutor(host="example.test", user="botmgr", key="/tmp/key")
    writes: list[tuple[str, int, bool]] = []
    commands: list[tuple[str, ...]] = []

    async def fake_write(path, data, *, mode=0o600, atomic=True):
        writes.append((str(path), mode, atomic))

    async def fake_run(argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        commands.append(args)
        return ExecResult(args, 0, "", "")

    monkeypatch.setattr(ssh, "write_bytes", fake_write)
    monkeypatch.setattr(ssh, "run", fake_run)

    await ssh.write_privileged_bytes("/etc/botman-agent/config.yaml", b"a", mode=0o640)
    await ssh.write_privileged_bytes("/etc/botman-agent/config.yaml", b"b", mode=0o640)

    assert writes[0][0] != writes[1][0]
    assert all(path.startswith("/tmp/botman-upload-") for path, _, _ in writes)
    assert all(mode == 0o600 and atomic is False for _, mode, atomic in writes)
    assert any(cmd[:4] == ("sudo", "chmod", "0640", "--") for cmd in commands)
    assert any(cmd[:3] == ("sudo", "mv", "--") for cmd in commands)


@pytest.mark.asyncio
async def test_privileged_write_propagates_chmod_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = SSHExecutor(host="example.test", user="botmgr", key="/tmp/key")

    async def fake_write(path, data, *, mode=0o600, atomic=True):
        return None

    async def fake_run(argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        if args[:2] == ("sudo", "chmod"):
            result = ExecResult(args, 1, "", "denied")
            if check:
                raise CommandError(result)
            return result
        return ExecResult(args, 0, "", "")

    monkeypatch.setattr(ssh, "write_bytes", fake_write)
    monkeypatch.setattr(ssh, "run", fake_run)

    with pytest.raises(CommandError):
        await ssh.write_privileged_bytes("/etc/botman-agent/config.yaml", b"secret")

class FakeRemoteFile:
    def __init__(self, writes: list[bytes]) -> None:
        self.writes = writes

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def write(self, data: bytes):
        self.writes.append(data)


class FakeSFTP:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.renames: list[tuple[str, str]] = []
        self.chmods: list[tuple[str, int]] = []
        self.removes: list[str] = []
        self.writes: list[bytes] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def open(self, path: str, mode: str):
        self.opened.append(path)
        return FakeRemoteFile(self.writes)

    async def chmod(self, path: str, mode: int):
        self.chmods.append((path, mode))

    async def posix_rename(self, src: str, dst: str):
        self.renames.append((src, dst))

    async def remove(self, path: str):
        self.removes.append(path)


class FakeSFTPConnection(FakeConnection):
    def __init__(self, sftp: FakeSFTP) -> None:
        super().__init__()
        self.sftp = sftp

    def start_sftp_client(self):
        return self.sftp


@pytest.mark.asyncio
async def test_sftp_atomic_write_uses_unique_temp_and_applies_final_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sftp = FakeSFTP()
    conns = [FakeSFTPConnection(sftp), FakeSFTPConnection(sftp)]

    async def fake_connect(self):
        return conns.pop(0)

    ssh = SSHExecutor(host="example.test", user="botmgr", key="/tmp/key")
    monkeypatch.setattr(SSHExecutor, "_connect", fake_connect)

    await ssh.write_bytes("/srv/botman/file", b"one", mode=0o640)
    await ssh.write_bytes("/srv/botman/file", b"two", mode=0o640)

    assert len(sftp.renames) == 2
    assert sftp.renames[0][0] != sftp.renames[1][0]
    assert all(src.startswith("/srv/botman/.file.botman-") for src, _ in sftp.renames)
    assert all(dst == "/srv/botman/file" for _, dst in sftp.renames)
    assert ("/srv/botman/file", 0o640) in sftp.chmods
    assert sftp.writes == [b"one", b"two"]


@pytest.mark.asyncio
async def test_privileged_write_propagates_move_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = SSHExecutor(host="example.test", user="botmgr", key="/tmp/key")

    async def fake_write(path, data, *, mode=0o600, atomic=True):
        return None

    async def fake_run(argv, *, timeout=None, check=False):
        args = tuple(str(x) for x in argv)
        if args[:2] == ("sudo", "mv"):
            result = ExecResult(args, 1, "", "move denied")
            if check:
                raise CommandError(result)
            return result
        return ExecResult(args, 0, "", "")

    monkeypatch.setattr(ssh, "write_bytes", fake_write)
    monkeypatch.setattr(ssh, "run", fake_run)

    with pytest.raises(CommandError):
        await ssh.write_privileged_bytes("/etc/botman-agent/config.yaml", b"secret")
