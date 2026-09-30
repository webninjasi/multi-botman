"""Short-lived journald exporter used by central `/logs` commands."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal

from .config import AgentConfig, AgentConfigError, load_agent_config
from .journal import JournalRecord, JournalUnavailableError

ExportFormat = Literal["human", "jsonl"]
DEFAULT_AGENT_CONFIG = Path("/var/lib/botman-log-agent/config.yaml")


class ExportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExportManifest:
    app: str
    identifier: str
    format: ExportFormat
    count: int
    files: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "app": self.app,
                "identifier": self.identifier,
                "format": self.format,
                "count": self.count,
                "files": list(self.files),
            },
            separators=(",", ":"),
        )


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _record_from_entry(entry: Any) -> JournalRecord:
    return JournalRecord(
        message=_decode(entry.data.get("MESSAGE", "")),
        cursor=_decode(entry.cursor),
        realtime_usec=int(entry.get_realtime_usec()),
    )


class CysystemdHistoricalReader:
    """Finite, filtered reads from the retained system journal."""

    def __init__(
        self,
        identifier: str,
        *,
        reader_factory: Callable[[], Any] | None = None,
        api: tuple[Any, Any] | None = None,
    ):
        self.identifier = identifier
        self._reader_factory = reader_factory
        self._api = api

    def _load_api(self) -> tuple[Any, Any, Callable[[], Any]]:
        if self._api is not None and self._reader_factory is not None:
            mode, rule = self._api
            return mode, rule, self._reader_factory
        try:
            from cysystemd.reader import (  # type: ignore[import-not-found]
                JournalOpenMode,
                JournalReader,
                Rule,
            )
        except ImportError as exc:  # pragma: no cover - Linux target dependency.
            raise JournalUnavailableError(
                "cysystemd 2.x is required to export Botman logs"
            ) from exc
        return JournalOpenMode, Rule, JournalReader

    def _open(self) -> Any:
        mode, rule_type, factory = self._load_api()
        reader = factory()
        reader.open(mode.SYSTEM)
        reader.add_filter(rule_type("SYSLOG_IDENTIFIER", self.identifier))
        reader.data_threshold = 0
        return reader

    def iter_range(self, since_usec: int, until_usec: int) -> Iterator[JournalRecord]:
        if since_usec < 0 or until_usec < 0 or until_usec < since_usec:
            raise ValueError("invalid realtime range")
        reader = self._open()
        try:
            reader.seek_realtime_usec(since_usec)
            for entry in reader:
                record = _record_from_entry(entry)
                if record.realtime_usec < since_usec:
                    continue
                if record.realtime_usec > until_usec:
                    break
                yield record
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()

    def tail(self, count: int) -> list[JournalRecord]:
        if count < 1:
            raise ValueError("tail count must be positive")
        # A bounded deque keeps memory fixed while avoiding assumptions about
        # cysystemd's current-entry semantics after seek_tail()/previous().
        recent: deque[JournalRecord] = deque(maxlen=count)
        reader = self._open()
        try:
            reader.seek_head()
            for entry in reader:
                recent.append(_record_from_entry(entry))
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
        return list(recent)


def _utc_iso(realtime_usec: int) -> str:
    seconds, micros = divmod(realtime_usec, 1_000_000)
    value = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=micros)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def render_record(
    record: JournalRecord,
    *,
    app: str,
    identifier: str,
    format: ExportFormat,
) -> bytes:
    if format == "jsonl":
        return (
            json.dumps(
                {
                    "timestamp": _utc_iso(record.realtime_usec),
                    "app": app,
                    "identifier": identifier,
                    "message": record.message,
                    "cursor": record.cursor,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    if format != "human":
        raise ValueError(f"unsupported export format: {format}")
    message = record.message
    suffix = "" if message.endswith("\n") else "\n"
    return (
        f"[{_utc_iso(record.realtime_usec)}] app={app} identifier={identifier}\n"
        f"{message}{suffix}\n"
    ).encode("utf-8")


class GzipPartWriter:
    """Bounded gzip writer whose every produced part respects max_part_bytes."""

    def __init__(
        self,
        output_dir: Path,
        *,
        app: str,
        format: ExportFormat,
        max_part_bytes: int,
    ):
        if max_part_bytes < 512:
            raise ValueError("max_part_bytes must be at least 512")
        self.output_dir = output_dir
        self.app = app
        self.format = format
        self.max_part_bytes = max_part_bytes
        self._current = bytearray()
        self._paths: list[Path] = []

    @property
    def extension(self) -> str:
        return "log.gz" if self.format == "human" else "jsonl.gz"

    def _flush(self) -> None:
        if not self._current:
            return
        index = len(self._paths) + 1
        path = self.output_dir / f"{self.app}.part{index:04d}.{self.extension}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._current)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        self._paths.append(path)
        self._current.clear()

    def _compressed_chunks(self, raw: bytes) -> list[bytes]:
        packed = gzip.compress(raw, compresslevel=6, mtime=0)
        if len(packed) <= self.max_part_bytes:
            return [packed]
        if len(raw) <= 1:
            raise ExportError("part budget is too small for gzip framing")
        # Rendered records are UTF-8. Split on character boundaries so each
        # independently downloadable gzip part remains valid UTF-8 even when
        # one exceptionally large record must be file-split.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            midpoint = len(raw) // 2
            return self._compressed_chunks(raw[:midpoint]) + self._compressed_chunks(raw[midpoint:])
        if len(text) <= 1:
            raise ExportError("part budget is too small for one UTF-8 character")
        midpoint = len(text) // 2
        return self._compressed_chunks(text[:midpoint].encode("utf-8")) + self._compressed_chunks(
            text[midpoint:].encode("utf-8")
        )

    def add(self, raw_record: bytes) -> None:
        members = self._compressed_chunks(raw_record)
        if len(members) > 1:
            self._flush()
            for member in members:
                self._current.extend(member)
                self._flush()
            return
        member = members[0]
        if self._current and len(self._current) + len(member) > self.max_part_bytes:
            self._flush()
        self._current.extend(member)

    def finish(self) -> tuple[Path, ...]:
        if self._current:
            self._flush()
        if not self._paths:
            self._current.extend(gzip.compress(b"", compresslevel=6, mtime=0))
            self._flush()
        return tuple(self._paths)


def export_records(
    records: Iterable[JournalRecord],
    *,
    app: str,
    identifier: str,
    format: ExportFormat,
    output_dir: Path,
    max_part_bytes: int,
) -> ExportManifest:
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    writer = GzipPartWriter(
        output_dir,
        app=app,
        format=format,
        max_part_bytes=max_part_bytes,
    )
    count = 0
    for record in records:
        writer.add(render_record(record, app=app, identifier=identifier, format=format))
        count += 1
    paths = writer.finish()
    return ExportManifest(app, identifier, format, count, tuple(str(path) for path in paths))


def parse_utc(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamps must include Z or an explicit offset")
    parsed = parsed.astimezone(UTC)
    return int(parsed.timestamp() * 1_000_000)


def _app_identifier(config: AgentConfig, app: str) -> str:
    try:
        return config.apps[app].identifier
    except KeyError as exc:
        raise ExportError(f"app is not defined in protected agent config: {app}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Botman app logs from journald")
    parser.add_argument("--config", default=str(DEFAULT_AGENT_CONFIG))
    parser.add_argument("--app", required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--tail", type=int)
    selector.add_argument("--since-utc")
    parser.add_argument("--until-utc")
    parser.add_argument("--format", choices=("human", "jsonl"), default="human")
    parser.add_argument("--output-dir")
    parser.add_argument("--max-part-bytes", type=int, default=7_500_000)
    parser.add_argument("--stdout", action="store_true")
    return parser


def run_cli(argv: list[str] | None = None, *, reader_factory=None, api=None) -> int:
    args = build_parser().parse_args(argv)
    if args.tail is not None:
        if args.tail < 1 or args.tail > 500:
            raise ExportError("--tail must be between 1 and 500")
        if args.until_utc:
            raise ExportError("--until-utc cannot be used with --tail")
    else:
        if not args.until_utc:
            raise ExportError("--until-utc is required with --since-utc")
    if args.stdout and args.output_dir:
        raise ExportError("choose either --stdout or --output-dir")
    if not args.stdout and not args.output_dir:
        raise ExportError("--output-dir is required unless --stdout is used")

    config = load_agent_config(args.config)
    identifier = _app_identifier(config, args.app)
    reader = CysystemdHistoricalReader(identifier, reader_factory=reader_factory, api=api)
    if args.tail is not None:
        records: Iterable[JournalRecord] = reader.tail(args.tail)
    else:
        since = parse_utc(args.since_utc)
        until = parse_utc(args.until_utc)
        if until < since:
            raise ExportError("--until-utc must not be earlier than --since-utc")
        records = reader.iter_range(since, until)

    if args.stdout:
        for record in records:
            sys.stdout.buffer.write(
                render_record(record, app=args.app, identifier=identifier, format=args.format)
            )
        return 0

    manifest = export_records(
        records,
        app=args.app,
        identifier=identifier,
        format=args.format,
        output_dir=Path(args.output_dir),
        max_part_bytes=args.max_part_bytes,
    )
    sys.stdout.write(manifest.to_json() + "\n")
    return 0


def main() -> None:
    try:
        raise SystemExit(run_cli())
    except (AgentConfigError, ExportError, JournalUnavailableError, ValueError) as exc:
        print(f"botman-log-export: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
