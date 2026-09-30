import json
from datetime import UTC

import pytest

from botman.executor import ExecResult
from botman.logs import HistoricalLogError, HistoricalLogsService, parse_local_time
from botman.models import BotmanConfig


def config():
    return BotmanConfig.model_validate(
        {
            "settings": {"timezone": "Europe/Istanbul"},
            "servers": {"vps1": {"type": "local"}},
            "stacks": {"s1": {"server": "vps1", "channel_id": "10", "project_name": "p1"}},
            "apps": {
                "app-a": {
                    "stack": "s1",
                    "service": "app-a",
                    "log_identifier": "tag-a",
                    "git": {"repo_url": "git@example.com:a.git"},
                }
            },
        }
    )


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.files = {}
        self.manifest = None

    async def run(self, argv, *, timeout=None, check=False):
        self.calls.append(tuple(argv))
        if "--tail" in argv:
            return ExecResult(tuple(argv), 0, "tail output", "")
        if "botman-log-export" in argv[0]:
            out = argv[argv.index("--output-dir") + 1]
            path = f"{out}/app-a.part0001.log.gz"
            self.files[path] = b"gzip-data"
            payload = self.manifest or {
                "app": "s1.app-a",
                "identifier": "tag-a",
                "format": "human",
                "count": 3,
                "files": [path],
            }
            return ExecResult(tuple(argv), 0, json.dumps(payload), "")
        return ExecResult(tuple(argv), 0, "", "")

    async def read_bytes(self, path):
        return self.files[str(path)]


def test_parse_local_time_uses_zoneinfo_and_rejects_dst_edges():
    ist = parse_local_time("2026-09-30 12:00", "Europe/Istanbul")
    assert ist.astimezone(UTC).hour == 9
    with pytest.raises(ValueError, match="does not exist"):
        parse_local_time("2026-03-08 02:30", "America/New_York")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_local_time("2026-11-01 01:30", "America/New_York")


@pytest.mark.asyncio
async def test_tail_authorizes_before_executor_access():
    made = []
    service = HistoricalLogsService(config(), executor_factory=lambda resolved: made.append(resolved) or FakeExecutor())
    with pytest.raises(PermissionError):
        await service.tail(app_name="app-a", channel_id=999, lines=10)
    assert made == []


@pytest.mark.asyncio
async def test_tail_uses_protected_helper_path():
    executor = FakeExecutor()
    service = HistoricalLogsService(config(), executor_factory=lambda _: executor)
    output = await service.tail(app_name="app-a", channel_id=10, lines=25)
    assert output == "tail output"
    call = executor.calls[0]
    assert call[0] == "/opt/botman-agent-venv/bin/botman-log-export"
    assert call[call.index("--config") + 1] == "/var/lib/botman-log-agent/config.yaml"
    assert call[call.index("--app") + 1] == "s1.app-a"
    assert call[call.index("--tail") + 1] == "25"


@pytest.mark.asyncio
async def test_tail_uses_configured_exporter_and_agent_config_paths():
    executor = FakeExecutor()
    cfg = config()
    cfg.settings.log_export_bin = "/custom/bin/botman-log-export"
    cfg.settings.agent_config_path = "/custom/botman-agent/config.yaml"
    service = HistoricalLogsService(cfg, executor_factory=lambda _: executor)

    await service.tail(app_name="app-a", channel_id=10, lines=10)

    call = executor.calls[0]
    assert call[0] == "/custom/bin/botman-log-export"
    assert call[call.index("--config") + 1] == "/custom/botman-agent/config.yaml"


@pytest.mark.asyncio
async def test_download_converts_local_time_downloads_and_cleans():
    executor = FakeExecutor()
    service = HistoricalLogsService(config(), executor_factory=lambda _: executor)
    result = await service.download(
        app_name="app-a",
        channel_id=10,
        from_local="2026-09-30 10:00",
        to_local="2026-09-30 11:00",
        format="human",
        max_part_bytes=1024,
    )
    assert result.count == 3
    assert result.artifacts[0].data == b"gzip-data"
    export_call = executor.calls[0]
    assert export_call[export_call.index("--since-utc") + 1] == "2026-09-30T07:00:00Z"
    assert export_call[export_call.index("--until-utc") + 1] == "2026-09-30T08:00:00Z"
    assert executor.calls[-1][:3] == ("rm", "-rf", "--")


@pytest.mark.asyncio
async def test_download_rejects_manifest_path_escape_and_still_cleans():
    executor = FakeExecutor()
    executor.manifest = {
        "app": "s1.app-a",
        "identifier": "tag-a",
        "format": "human",
        "count": 1,
        "files": ["/etc/passwd"],
    }
    service = HistoricalLogsService(config(), executor_factory=lambda _: executor)
    with pytest.raises(HistoricalLogError, match="outside"):
        await service.download(
            app_name="app-a",
            channel_id=10,
            from_local="2026-09-30 10:00",
            to_local="2026-09-30 11:00",
        )
    assert executor.calls[-1][:3] == ("rm", "-rf", "--")
