"""Target-host readiness checks for Botman logging and Compose management."""

from __future__ import annotations

import argparse
import configparser
import grp
import importlib.metadata
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_AGENT_ROOT = Path("/var/lib/botman-log-agent")
DEFAULT_AGENT_CONFIG = DEFAULT_AGENT_ROOT / "config.yaml"
DEFAULT_AGENT_STATE = DEFAULT_AGENT_ROOT / "state"
DEFAULT_JOURNAL_DROPIN = Path("/etc/systemd/journald.conf.d/90-botman.conf")
DEFAULT_JOURNAL_DIR = Path("/var/log/journal")
DEFAULT_AGENT_UNIT = Path("/etc/systemd/system/botman-log-agent.service")
AGENT_USER = "botman-log-agent"
AGENT_SERVICE = "botman-log-agent.service"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )


def _groups_for_user(user: str) -> set[str]:
    account = pwd.getpwnam(user)
    names = {grp.getgrgid(account.pw_gid).gr_name}
    for entry in grp.getgrall():
        if user in entry.gr_mem:
            names.add(entry.gr_name)
    return names


def _check_command(name: str, argv: Sequence[str], runner: Runner) -> CheckResult:
    try:
        result = runner(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(name, False, f"failed to execute: {exc}")
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit {result.returncode}"
    return CheckResult(name, result.returncode == 0, detail)


def _check_journal_dropin(path: Path, expected_max_use: str) -> CheckResult:
    if not path.is_file():
        return CheckResult("journald policy", False, f"missing {path}")
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        return CheckResult("journald policy", False, f"cannot parse {path}: {exc}")
    if not parser.has_section("Journal"):
        return CheckResult("journald policy", False, f"{path} has no [Journal] section")
    storage = parser.get("Journal", "Storage", fallback="").strip().lower()
    max_use = parser.get("Journal", "SystemMaxUse", fallback="").strip().upper()
    compress = parser.get("Journal", "Compress", fallback="").strip().lower()
    expected = expected_max_use.strip().upper()
    problems: list[str] = []
    if storage != "persistent":
        problems.append("Storage must be persistent")
    if max_use != expected:
        problems.append(f"SystemMaxUse is {max_use or 'unset'}, expected {expected}")
    if compress not in {"yes", "true", "1", "on"}:
        problems.append("Compress must be enabled")
    if problems:
        return CheckResult("journald policy", False, "; ".join(problems))
    return CheckResult("journald policy", True, f"persistent, cap {expected}, compression enabled")


def _check_directory(
    name: str,
    path: Path,
    *,
    expected_mode: int | None = None,
    expected_user: str | None = None,
    expected_group: str | None = None,
) -> CheckResult:
    try:
        info = path.stat()
    except OSError as exc:
        return CheckResult(name, False, f"cannot stat {path}: {exc}")
    if not stat.S_ISDIR(info.st_mode):
        return CheckResult(name, False, f"{path} is not a directory")
    problems: list[str] = []
    actual_mode = stat.S_IMODE(info.st_mode)
    if expected_mode is not None and actual_mode != expected_mode:
        problems.append(f"mode {actual_mode:04o}, expected {expected_mode:04o}")
    if expected_user is not None:
        try:
            actual_user = pwd.getpwuid(info.st_uid).pw_name
        except KeyError:
            actual_user = str(info.st_uid)
        if actual_user != expected_user:
            problems.append(f"owner {actual_user}, expected {expected_user}")
    if expected_group is not None:
        try:
            actual_group = grp.getgrgid(info.st_gid).gr_name
        except KeyError:
            actual_group = str(info.st_gid)
        if actual_group != expected_group:
            problems.append(f"group {actual_group}, expected {expected_group}")
    if problems:
        return CheckResult(name, False, "; ".join(problems))
    return CheckResult(name, True, f"{path} mode {actual_mode:04o}")


def _check_user_groups(user: str) -> CheckResult:
    required = {AGENT_USER, "systemd-journal"}
    try:
        groups = _groups_for_user(user)
    except KeyError:
        return CheckResult("management user groups", False, f"user {user!r} does not exist")
    missing = sorted(required - groups)
    if missing:
        return CheckResult(
            "management user groups",
            False,
            f"{user} is missing: {', '.join(missing)}",
        )
    return CheckResult("management user groups", True, f"{user}: {', '.join(sorted(required))}")


def _check_agent_user() -> CheckResult:
    try:
        groups = _groups_for_user(AGENT_USER)
    except KeyError:
        return CheckResult("agent user", False, f"user {AGENT_USER!r} does not exist")
    if "systemd-journal" not in groups:
        return CheckResult("agent user", False, f"{AGENT_USER} is not in systemd-journal")
    return CheckResult("agent user", True, f"{AGENT_USER} has systemd-journal access")


def _check_cysystemd() -> CheckResult:
    try:
        version = importlib.metadata.version("cysystemd")
        major = int(version.split(".", 1)[0])
        from cysystemd.async_reader import AsyncJournalReader  # type: ignore[import-not-found]
        from cysystemd.reader import JournalOpenMode, JournalReader, Rule  # type: ignore[import-not-found]
    except (ImportError, importlib.metadata.PackageNotFoundError, ValueError) as exc:
        return CheckResult("cysystemd", False, f"unavailable: {exc}")
    if major != 2:
        return CheckResult("cysystemd", False, f"version {version}; Botman requires 2.x")
    # Referencing all required symbols catches partial/broken installs without opening the journal here.
    _ = (AsyncJournalReader, JournalOpenMode, JournalReader, Rule)
    return CheckResult("cysystemd", True, f"version {version}")


def _check_journal_open() -> CheckResult:
    try:
        from cysystemd.reader import JournalOpenMode, JournalReader  # type: ignore[import-not-found]

        reader = JournalReader()
        reader.open(JournalOpenMode.SYSTEM)
        close = getattr(reader, "close", None)
        if callable(close):
            close()
    except Exception as exc:  # Linux/cysystemd errors vary by host/version.
        return CheckResult("system journal access", False, f"cannot open system journal: {exc}")
    return CheckResult("system journal access", True, "system journal opened successfully")


def _check_sudo_service_control(systemctl: str, runner: Runner) -> CheckResult:
    argv = [
        "sudo",
        "-n",
        systemctl,
        "status",
        "--no-pager",
        "--lines=20",
        AGENT_SERVICE,
    ]
    try:
        result = runner(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("sudo service control", False, f"failed to execute: {exc}")
    # systemctl status returns 3 for an installed but inactive service. Both 0 and 3
    # prove the exact non-interactive sudoers command was authorized.
    if result.returncode in {0, 3}:
        state = "active" if result.returncode == 0 else "installed but inactive"
        return CheckResult("sudo service control", True, state)
    detail = (result.stderr or result.stdout).strip().splitlines()
    return CheckResult(
        "sudo service control",
        False,
        detail[0] if detail else f"exit {result.returncode}",
    )


def run_preflight(
    *,
    management_user: str,
    compose_argv: tuple[str, ...],
    expected_journal_max_use: str = "1G",
    journal_dropin: Path = DEFAULT_JOURNAL_DROPIN,
    journal_dir: Path = DEFAULT_JOURNAL_DIR,
    agent_root: Path = DEFAULT_AGENT_ROOT,
    agent_state: Path = DEFAULT_AGENT_STATE,
    agent_unit: Path = DEFAULT_AGENT_UNIT,
    agent_config: Path = DEFAULT_AGENT_CONFIG,
    app: str | None = None,
    require_active: bool = False,
    runner: Runner = _run,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"

    checks.append(_check_command("systemd", [systemctl, "--version"], runner))
    checks.append(_check_command("Compose runtime", [*compose_argv, "version"], runner))
    checks.append(_check_agent_user())
    checks.append(_check_user_groups(management_user))
    checks.append(
        _check_directory(
            "agent config directory",
            agent_root,
            expected_mode=0o3770,
            expected_user="root",
            expected_group=AGENT_USER,
        )
    )
    checks.append(
        _check_directory(
            "agent state directory",
            agent_state,
            expected_mode=0o700,
            expected_user=AGENT_USER,
            expected_group=AGENT_USER,
        )
    )
    checks.append(_check_journal_dropin(journal_dropin, expected_journal_max_use))
    checks.append(_check_directory("persistent journal directory", journal_dir))
    checks.append(_check_cysystemd())
    checks.append(_check_journal_open())

    if agent_unit.is_file():
        checks.append(CheckResult("agent systemd unit", True, str(agent_unit)))
    else:
        checks.append(CheckResult("agent systemd unit", False, f"missing {agent_unit}"))
    checks.append(_check_command("agent unit enabled", [systemctl, "is-enabled", AGENT_SERVICE], runner))
    checks.append(_check_sudo_service_control(systemctl, runner))

    if require_active:
        checks.append(_check_command("agent active", [systemctl, "is-active", AGENT_SERVICE], runner))

    if app is not None:
        exporter = shutil.which("botman-log-export")
        if exporter is None:
            sibling = Path(sys.executable).with_name("botman-log-export")
            exporter = str(sibling) if sibling.is_file() else None
        if exporter is None:
            checks.append(CheckResult("app journal export", False, "botman-log-export is not installed beside this interpreter or on PATH"))
        elif not agent_config.is_file():
            checks.append(CheckResult("app journal export", False, f"missing {agent_config}"))
        else:
            checks.append(
                _check_command(
                    "app journal export",
                    [
                        exporter,
                        "--config",
                        str(agent_config),
                        "--app",
                        app,
                        "--tail",
                        "1",
                        "--format",
                        "human",
                        "--stdout",
                    ],
                    runner,
                )
            )
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check whether a target VPS is ready for Botman Compose and journald logging"
    )
    parser.add_argument(
        "--management-user",
        default=pwd.getpwuid(os.getuid()).pw_name,
        help="Botman management identity on this target (botmgr or botman)",
    )
    parser.add_argument(
        "--compose-command",
        default="docker compose",
        help="Compose command, quoted as one shell-style string (for example 'podman compose')",
    )
    parser.add_argument("--journal-max-use", default="1G")
    parser.add_argument(
        "--app",
        help="optional stack-qualified app key (STACK.APP) to verify through botman-log-export",
    )
    parser.add_argument(
        "--require-active",
        action="store_true",
        help="also require botman-log-agent.service to be active (use after /config agent sync)",
    )
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        compose_argv = tuple(shlex.split(args.compose_command))
    except ValueError as exc:
        print(f"botman-target-preflight: invalid --compose-command: {exc}", file=sys.stderr)
        return 2
    if not compose_argv:
        print("botman-target-preflight: --compose-command cannot be empty", file=sys.stderr)
        return 2

    checks = run_preflight(
        management_user=args.management_user,
        compose_argv=compose_argv,
        expected_journal_max_use=args.journal_max_use,
        app=args.app,
        require_active=args.require_active,
    )
    width = max(len(check.name) for check in checks)
    for check in checks:
        marker = "PASS" if check.ok else "FAIL"
        print(f"{marker:4}  {check.name:<{width}}  {check.detail}")
    failed = sum(not check.ok for check in checks)
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":  # pragma: no cover
    main()
