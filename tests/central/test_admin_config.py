from __future__ import annotations

from pathlib import Path

import pytest

from botman.admin_config import AdminConfigService, AdminGuard, AdminAuthorizationError, parse_compose_argv
from botman.config import ConfigStore
from botman.models import SSHServerConfig


def test_admin_guard_and_compose_argv_parser() -> None:
    guard = AdminGuard(frozenset({10, 20}))
    guard.require(10)
    with pytest.raises(AdminAuthorizationError):
        guard.require(99)
    assert parse_compose_argv("sudo podman compose") == ("sudo", "podman", "compose")
    assert parse_compose_argv("docker compose") == ("docker", "compose")


@pytest.mark.asyncio
async def test_admin_onboarding_mutations_build_valid_config(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)

    await service.add_server(
        name="vps1",
        server_type="ssh",
        host="vps1.example.com",
        port=22,
        user="botmgr",
        key="/home/botman/.ssh/server-vps1",
        known_hosts="/home/botman/.ssh/known_hosts",
        compose_argv=("sudo", "podman", "compose"),
    )
    await service.add_stack(name="bots", server="vps1", channel_id=1234)
    await service.add_app(
        name="app-a",
        channel_id=1234,
        service="app-a",
        repo_url="git@github.com:owner/app-a.git",
        branch="main",
    )

    config = store.load()
    assert isinstance(config.servers["vps1"], SSHServerConfig)
    assert config.stacks["bots"].channel_id == "1234"
    assert config.stacks["bots"].project_name == "botman-bots"
    assert config.stacks["bots"].apps["app-a"].log_identifier == "botman-bots-app-a"
    assert config.stacks["bots"].apps["app-a"].git.deploy_key_path is None


@pytest.mark.asyncio
async def test_duplicate_admin_objects_are_rejected(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(name="local", server_type="local", compose_argv=("docker", "compose"))
    with pytest.raises(ValueError, match="already exists"):
        await service.add_server(name="local", server_type="local", compose_argv=("docker", "compose"))
