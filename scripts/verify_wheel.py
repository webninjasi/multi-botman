#!/usr/bin/env python3
"""Verify a built Botman wheel contains the expected packages and entry points."""

from __future__ import annotations

import argparse
import configparser
import sys
import zipfile
from pathlib import Path

REQUIRED_MODULES = {
    "botman/bot.py",
    "botman/deployment.py",
    "botman/commands/admin.py",
    "botman_agent/main.py",
    "botman_agent/export.py",
}
REQUIRED_SCRIPTS = {
    "botman": "botman.bot:main",
    "botman-log-agent": "botman_agent.main:main",
    "botman-log-export": "botman_agent.export:main",
}


def verify(path: Path) -> None:
    if not path.is_file() or path.suffix != ".whl":
        raise ValueError(f"not a wheel file: {path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = sorted(REQUIRED_MODULES - names)
        if missing:
            raise RuntimeError(f"wheel is missing required modules: {', '.join(missing)}")
        entry_files = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entry_files) != 1:
            raise RuntimeError("wheel must contain exactly one dist-info entry_points.txt")
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_files[0]).decode("utf-8"))
        scripts = dict(parser.items("console_scripts")) if parser.has_section("console_scripts") else {}
        for name, target in REQUIRED_SCRIPTS.items():
            if scripts.get(name) != target:
                raise RuntimeError(
                    f"console script {name!r} points to {scripts.get(name)!r}, expected {target!r}"
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args(argv)
    try:
        verify(args.wheel)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"verify-wheel: {exc}", file=sys.stderr)
        return 2
    print(f"verified wheel: {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
