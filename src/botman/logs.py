"""Historical journald retrieval through the protected target-side exporter."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo

from .compose import executor_for_resolved_app
from .executor import Executor
from .models import BotmanConfig
from .routing import ResolvedApp, authorize_app_channel

AGENT_CONFIG_PATH = PurePosixPath("/var/lib/botman-log-agent/config.yaml")
LOG_EXPORT_BIN = PurePosixPath("/opt/botman-agent/.venv/bin/botman-log-export")
EXPORT_ROOT = PurePosixPath("/var/tmp")
DEFAULT_EXPORT_PART_BYTES = 7_500_000
MAX_EXPORT_PARTS = 20
LogFormat = Literal["human", "jsonl"]
ExecutorFactory = Callable[[ResolvedApp], Executor]


class HistoricalLogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LogArtifact:
    filename: str
    data: bytes


@dataclass(frozen=True, slots=True)
class DownloadResult:
    app_name: str
    count: int
    artifacts: tuple[LogArtifact, ...]


def parse_local_time(value: str, timezone: str) -> datetime:
    """Parse an operator-entered local time and reject DST gaps/ambiguity."""

    text = value.strip().replace(" ", "T", 1)
    try:
        naive = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("time must look like YYYY-MM-DD HH:MM[:SS]") from exc
    if naive.tzinfo is not None:
        raise ValueError("enter local wall time without a UTC offset")
    zone = ZoneInfo(timezone)
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        roundtrip = aware.astimezone(UTC).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == naive:
            candidates.append(aware)
    if not candidates:
        raise ValueError(f"local time does not exist in timezone {timezone}")
    offsets = {candidate.utcoffset() for candidate in candidates}
    if len(offsets) > 1:
        raise ValueError(f"local time is ambiguous in timezone {timezone}; use a different time")
    return candidates[0]


def utc_cli_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class HistoricalLogsService:
    def __init__(
        self,
        config: BotmanConfig,
        *,
        executor_factory: ExecutorFactory = executor_for_resolved_app,
    ):
        self.config = config
        self.executor_factory = executor_factory

    def authorize(self, app_name: str, channel_id: str | int) -> ResolvedApp:
        return authorize_app_channel(self.config, app_name, channel_id)

    async def tail(self, *, app_name: str, channel_id: str | int, lines: int = 100) -> str:
        if not 1 <= lines <= 500:
            raise ValueError("lines must be between 1 and 500")
        resolved = self.authorize(app_name, channel_id)
        result = await self.executor_factory(resolved).run(
            [
                str(LOG_EXPORT_BIN),
                "--config",
                str(AGENT_CONFIG_PATH),
                "--app",
                app_name,
                "--tail",
                str(lines),
                "--format",
                "human",
                "--stdout",
            ],
            timeout=60,
            check=True,
        )
        return result.stdout

    async def download(
        self,
        *,
        app_name: str,
        channel_id: str | int,
        from_local: str,
        to_local: str,
        format: LogFormat = "human",
        max_part_bytes: int = DEFAULT_EXPORT_PART_BYTES,
    ) -> DownloadResult:
        if format not in ("human", "jsonl"):
            raise ValueError("format must be human or jsonl")
        if max_part_bytes < 512:
            raise ValueError("attachment budget is too small")
        resolved = self.authorize(app_name, channel_id)
        start = parse_local_time(from_local, self.config.settings.timezone)
        end = parse_local_time(to_local, self.config.settings.timezone)
        if end <= start:
            raise ValueError("to_local must be later than from_local")

        executor = self.executor_factory(resolved)
        read_bytes = getattr(executor, "read_bytes", None)
        if not callable(read_bytes):
            raise HistoricalLogError("target executor cannot download export files")
        remote_dir = EXPORT_ROOT / f"botman-export-{uuid.uuid4().hex}"
        try:
            result = await executor.run(
                [
                    str(LOG_EXPORT_BIN),
                    "--config",
                    str(AGENT_CONFIG_PATH),
                    "--app",
                    app_name,
                    "--since-utc",
                    utc_cli_time(start),
                    "--until-utc",
                    utc_cli_time(end),
                    "--format",
                    format,
                    "--output-dir",
                    str(remote_dir),
                    "--max-part-bytes",
                    str(max_part_bytes),
                ],
                timeout=300,
                check=True,
            )
            manifest = self._parse_manifest(result.stdout, remote_dir, app_name, format)
            artifacts: list[LogArtifact] = []
            for path in manifest["files"]:
                data = await read_bytes(path)
                if len(data) > max_part_bytes:
                    raise HistoricalLogError(
                        f"exporter produced oversized part {PurePosixPath(path).name}"
                    )
                artifacts.append(LogArtifact(PurePosixPath(path).name, data))
            return DownloadResult(app_name, int(manifest["count"]), tuple(artifacts))
        finally:
            # The directory name is generated locally and never incorporates
            # user input. Cleanup is best-effort so the original error wins.
            try:
                await executor.run(["rm", "-rf", "--", str(remote_dir)], timeout=30, check=False)
            except Exception:
                pass

    @staticmethod
    def _parse_manifest(
        stdout: str,
        remote_dir: PurePosixPath,
        app_name: str,
        format: LogFormat,
    ) -> dict:
        try:
            payload = json.loads(stdout.strip())
        except (json.JSONDecodeError, TypeError) as exc:
            raise HistoricalLogError("target exporter returned an invalid manifest") from exc
        if not isinstance(payload, dict):
            raise HistoricalLogError("target exporter manifest must be an object")
        if payload.get("app") != app_name or payload.get("format") != format:
            raise HistoricalLogError("target exporter manifest identity mismatch")
        count = payload.get("count")
        files = payload.get("files")
        if not isinstance(count, int) or count < 0 or not isinstance(files, list) or not files:
            raise HistoricalLogError("target exporter manifest is incomplete")
        if len(files) > MAX_EXPORT_PARTS:
            raise HistoricalLogError(
                f"export produced {len(files)} parts; narrow the requested time range"
            )
        normalized: list[str] = []
        for raw in files:
            if not isinstance(raw, str):
                raise HistoricalLogError("target exporter returned a non-string path")
            path = PurePosixPath(raw)
            if path.parent != remote_dir or path.name in {"", ".", ".."}:
                raise HistoricalLogError("target exporter returned a path outside its temp directory")
            normalized.append(str(path))
        payload["files"] = normalized
        return payload
