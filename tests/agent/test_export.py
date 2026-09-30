import gzip
import json
from pathlib import Path

import pytest

from botman_agent.export import (
    CysystemdHistoricalReader,
    ExportError,
    export_records,
    parse_utc,
    render_record,
    run_cli,
)
from botman_agent.journal import JournalRecord


class Mode:
    SYSTEM = object()


class Rule:
    def __init__(self, field, value):
        self.field = field
        self.value = value


class Entry:
    def __init__(self, message, cursor, usec):
        self.data = {"MESSAGE": message}
        self.cursor = cursor
        self._usec = usec

    def get_realtime_usec(self):
        return self._usec


class Reader:
    entries = []
    instances = []

    def __init__(self):
        self.data_threshold = None
        self.filters = []
        self.start = 0
        self.closed = False
        self.__class__.instances.append(self)

    def open(self, mode):
        self.mode = mode

    def add_filter(self, rule):
        self.filters.append(rule)

    def seek_realtime_usec(self, value):
        self.start = value

    def seek_head(self):
        self.start = 0

    def __iter__(self):
        return iter([entry for entry in self.entries if entry._usec >= self.start])

    def close(self):
        self.closed = True


def test_historical_range_filters_boundaries_and_full_data():
    Reader.entries = [Entry("before", b"c0", 9), Entry("α\nβ", b"c1", 10), Entry("end", b"c2", 20), Entry("after", b"c3", 21)]
    Reader.instances = []
    reader = CysystemdHistoricalReader("tag", reader_factory=Reader, api=(Mode, Rule))
    records = list(reader.iter_range(10, 20))
    assert [r.message for r in records] == ["α\nβ", "end"]
    inst = Reader.instances[-1]
    assert inst.data_threshold == 0
    assert [(r.field, r.value) for r in inst.filters] == [("SYSLOG_IDENTIFIER", "tag")]
    assert inst.closed


def test_tail_keeps_only_requested_recent_entries():
    Reader.entries = [Entry(f"m{i}", f"c{i}", i) for i in range(10)]
    reader = CysystemdHistoricalReader("tag", reader_factory=Reader, api=(Mode, Rule))
    assert [r.message for r in reader.tail(3)] == ["m7", "m8", "m9"]


def test_render_human_and_jsonl_preserve_multiline_message():
    record = JournalRecord("one\ntwo\n", "cursor", 1_700_000_000_123_456)
    human = render_record(record, app="app-a", identifier="tag-a", format="human").decode()
    assert "one\ntwo\n" in human
    payload = json.loads(render_record(record, app="app-a", identifier="tag-a", format="jsonl"))
    assert payload["message"] == "one\ntwo\n"
    assert payload["cursor"] == "cursor"
    assert payload["timestamp"].endswith("Z")


def test_export_parts_never_exceed_budget_and_recompose(tmp_path):
    records = [
        JournalRecord("x" * 6000, "c1", 1),
        JournalRecord("second\nline", "c2", 2),
    ]
    out = tmp_path / "out"
    manifest = export_records(
        records,
        app="app-a",
        identifier="tag-a",
        format="jsonl",
        output_dir=out,
        max_part_bytes=600,
    )
    assert manifest.count == 2
    parts = [Path(name).read_bytes() for name in manifest.files]
    assert parts and all(len(part) <= 600 for part in parts)
    restored = b"".join(gzip.decompress(part) for part in parts)
    lines = restored.decode().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "x" * 6000
    assert json.loads(lines[1])["message"] == "second\nline"


def test_empty_export_still_creates_valid_gzip(tmp_path):
    manifest = export_records(
        [],
        app="app-a",
        identifier="tag-a",
        format="human",
        output_dir=tmp_path / "empty",
        max_part_bytes=1024,
    )
    assert manifest.count == 0
    assert gzip.decompress(Path(manifest.files[0]).read_bytes()) == b""


def test_parse_utc_requires_timezone_and_supports_z():
    assert parse_utc("1970-01-01T00:00:01Z") == 1_000_000
    with pytest.raises(ValueError, match="include Z"):
        parse_utc("1970-01-01T00:00:01")


def write_agent_config(path: Path):
    path.write_text(
        """
apps:
  app-a:
    identifier: tag-a
    enabled: false
"""
    )


def test_cli_rejects_app_not_in_protected_config(tmp_path):
    config = tmp_path / "config.yaml"
    write_agent_config(config)
    with pytest.raises(ExportError, match="not defined"):
        run_cli(
            ["--config", str(config), "--app", "other", "--tail", "1", "--stdout"],
            reader_factory=Reader,
            api=(Mode, Rule),
        )


def test_cli_range_writes_manifest(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    write_agent_config(config)
    Reader.entries = [Entry("hello", b"c1", 1_000_000)]
    output = tmp_path / "export"
    assert run_cli(
        [
            "--config", str(config),
            "--app", "app-a",
            "--since-utc", "1970-01-01T00:00:00Z",
            "--until-utc", "1970-01-01T00:00:02Z",
            "--format", "human",
            "--output-dir", str(output),
            "--max-part-bytes", "1024",
        ],
        reader_factory=Reader,
        api=(Mode, Rule),
    ) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["count"] == 1
    assert Path(manifest["files"][0]).is_file()
