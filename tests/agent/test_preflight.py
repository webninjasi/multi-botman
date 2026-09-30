from __future__ import annotations

import grp
import pwd
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from botman_agent import preflight


def result(code: int = 0, stdout: str = "ok\n", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_journal_dropin_requires_expected_policy(tmp_path: Path) -> None:
    path = tmp_path / "90-botman.conf"
    path.write_text("[Journal]\nStorage=persistent\nSystemMaxUse=1G\nCompress=yes\n")
    check = preflight._check_journal_dropin(path, "1G")
    assert check.ok

    path.write_text("[Journal]\nStorage=volatile\nSystemMaxUse=512M\nCompress=no\n")
    check = preflight._check_journal_dropin(path, "1G")
    assert not check.ok
    assert "Storage must be persistent" in check.detail
    assert "expected 1G" in check.detail
    assert "Compress must be enabled" in check.detail


def test_directory_check_validates_mode_owner_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state"
    path.mkdir()
    path.chmod(0o700)
    uid = path.stat().st_uid
    gid = path.stat().st_gid
    monkeypatch.setattr(pwd, "getpwuid", lambda value: SimpleNamespace(pw_name="agent") if value == uid else None)
    monkeypatch.setattr(grp, "getgrgid", lambda value: SimpleNamespace(gr_name="agent") if value == gid else None)

    check = preflight._check_directory(
        "state", path, expected_mode=0o700, expected_user="agent", expected_group="agent"
    )
    assert check.ok

    path.chmod(0o755)
    check = preflight._check_directory("state", path, expected_mode=0o700)
    assert not check.ok
    assert "expected 0700" in check.detail


def test_sudo_status_accepts_inactive_service() -> None:
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return result(3, stdout="inactive\n")

    check = preflight._check_sudo_service_control("/usr/bin/systemctl", runner)
    assert check.ok
    assert "inactive" in check.detail
    assert calls == [[
        "sudo", "-n", "/usr/bin/systemctl", "status", "--no-pager", "--lines=20",
        "botman-log-agent.service",
    ]]


def test_sudo_status_rejects_permission_failure() -> None:
    check = preflight._check_sudo_service_control(
        "/usr/bin/systemctl",
        lambda argv: result(1, stderr="sudo: a password is required\n"),
    )
    assert not check.ok
    assert "password" in check.detail


def test_cli_rejects_empty_compose_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert preflight.run_cli(["--compose-command", "   "]) == 2
    assert "cannot be empty" in capsys.readouterr().err


def test_cli_prints_summary_and_returns_failure(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        preflight,
        "run_preflight",
        lambda **kwargs: [
            preflight.CheckResult("one", True, "good"),
            preflight.CheckResult("two", False, "bad"),
        ],
    )
    assert preflight.run_cli(["--management-user", "botmgr"]) == 1
    output = capsys.readouterr().out
    assert "PASS" in output
    assert "FAIL" in output
    assert "1 passed, 1 failed" in output


def test_run_preflight_builds_runtime_and_export_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = tmp_path / "journal"
    journal.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    state = root / "state"
    state.mkdir()
    unit = tmp_path / "botman-log-agent.service"
    unit.write_text("unit")
    config = root / "config.yaml"
    config.write_text("apps: {}\n")
    dropin = tmp_path / "90-botman.conf"
    dropin.write_text("[Journal]\nStorage=persistent\nSystemMaxUse=1G\nCompress=yes\n")

    # Avoid making this orchestration test depend on the host's users, cysystemd or ownership.
    monkeypatch.setattr(preflight, "_check_agent_user", lambda: preflight.CheckResult("agent user", True, "ok"))
    monkeypatch.setattr(preflight, "_check_user_groups", lambda user: preflight.CheckResult("groups", True, user))
    monkeypatch.setattr(preflight, "_check_cysystemd", lambda: preflight.CheckResult("cysystemd", True, "2.x"))
    monkeypatch.setattr(preflight, "_check_journal_open", lambda: preflight.CheckResult("journal", True, "ok"))
    monkeypatch.setattr(
        preflight,
        "_check_directory",
        lambda name, path, **kwargs: preflight.CheckResult(name, True, str(path)),
    )
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/venv/bin/botman-log-export" if name == "botman-log-export" else "/usr/bin/systemctl")

    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if list(argv)[:2] == ["sudo", "-n"]:
            return result(3, "inactive\n")
        return result()

    checks = preflight.run_preflight(
        management_user="botmgr",
        compose_argv=("podman", "compose"),
        journal_dropin=dropin,
        journal_dir=journal,
        agent_root=root,
        agent_state=state,
        agent_unit=unit,
        agent_config=config,
        app="stack.app",
        require_active=True,
        runner=runner,
    )
    assert all(check.ok for check in checks)
    assert ["podman", "compose", "version"] in calls
    assert ["/usr/bin/systemctl", "is-active", "botman-log-agent.service"] in calls
    assert any(call[:2] == ["/venv/bin/botman-log-export", "--config"] for call in calls)
