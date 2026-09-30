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


@pytest.mark.asyncio
async def test_edit_server_preserves_unspecified_fields_and_can_clear_known_hosts(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(
        name="vps1",
        server_type="ssh",
        host="old.example.com",
        port=2222,
        user="botmgr",
        key="/keys/vps1",
        known_hosts="/keys/known_hosts",
        compose_argv=("sudo", "podman", "compose"),
    )

    await service.edit_server(name="vps1", host="new.example.com", clear_known_hosts=True)

    server = store.load().servers["vps1"]
    assert isinstance(server, SSHServerConfig)
    assert server.host == "new.example.com"
    assert server.port == 2222
    assert server.user == "botmgr"
    assert server.key == Path("/keys/vps1")
    assert server.known_hosts is None
    assert server.compose_argv == ("sudo", "podman", "compose")


@pytest.mark.asyncio
async def test_edit_server_type_switch_requires_ssh_identity(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(name="local", server_type="local", compose_argv=("docker", "compose"))

    with pytest.raises(ValueError, match="requires: host, user, key"):
        await service.edit_server(name="local", server_type="ssh")

    await service.edit_server(
        name="local",
        server_type="ssh",
        host="host.example.com",
        user="botmgr",
        key="/keys/local",
    )
    server = store.load().servers["local"]
    assert isinstance(server, SSHServerConfig)
    assert server.host == "host.example.com"
    assert server.port == 22


@pytest.mark.asyncio
async def test_stack_edit_resolves_stack_from_channel_and_is_safe_before_apps(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(name="one", server_type="local", compose_argv=("docker", "compose"))
    await service.add_server(name="two", server_type="local", compose_argv=("podman", "compose"))
    await service.add_stack(name="bots", server="one", channel_id=1234)

    await service.edit_stack(
        channel_id=1234,
        server="two",
        project_name="custom-project",
        compose_file="compose.yaml",
    )
    stack = store.load().stacks["bots"]
    assert stack.server == "two"
    assert stack.project_name == "custom-project"
    assert stack.compose_file == "compose.yaml"


@pytest.mark.asyncio
async def test_stack_identity_edit_rejected_after_apps_exist(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(name="one", server_type="local", compose_argv=("docker", "compose"))
    await service.add_server(name="two", server_type="local", compose_argv=("podman", "compose"))
    await service.add_stack(name="bots", server="one", channel_id=1234)
    await service.add_app(
        name="app",
        channel_id=1234,
        service="app",
        repo_url="git@github.com:owner/app.git",
    )

    with pytest.raises(ValueError, match="cannot be changed in place"):
        await service.edit_stack(channel_id=1234, server="two")


@pytest.mark.asyncio
async def test_app_edit_is_channel_scoped_and_preserves_deploy_key(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(name="local", server_type="local", compose_argv=("docker", "compose"))
    await service.add_stack(name="one", server="local", channel_id=111)
    await service.add_stack(name="two", server="local", channel_id=222)
    for channel, repo in ((111, "one"), (222, "two")):
        await service.add_app(
            name="app",
            channel_id=channel,
            service="app",
            repo_url=f"git@github.com:owner/{repo}.git",
        )

    def set_key(config):
        config.stacks["one"].apps["app"].git.deploy_key_path = Path("/keys/one/app")

    await store.mutate(set_key)
    await service.edit_app(
        name="app",
        channel_id=111,
        service="web",
        repo_url="git@github.com:owner/one-new.git",
        branch="stable",
        log_identifier="one-web",
    )

    config = store.load()
    edited = config.stacks["one"].apps["app"]
    untouched = config.stacks["two"].apps["app"]
    assert edited.service == "web"
    assert edited.git.repo_url == "git@github.com:owner/one-new.git"
    assert edited.git.branch == "stable"
    assert edited.git.deploy_key_path == Path("/keys/one/app")
    assert edited.log_identifier == "one-web"
    assert untouched.git.repo_url == "git@github.com:owner/two.git"


@pytest.mark.asyncio
async def test_app_log_identifier_edit_requires_live_logs_stopped(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(name="local", server_type="local", compose_argv=("docker", "compose"))
    await service.add_stack(name="bots", server="local", channel_id=1234)
    await service.add_app(
        name="app",
        channel_id=1234,
        service="app",
        repo_url="git@github.com:owner/app.git",
    )

    def enable_live(config):
        log = config.stacks["bots"].apps["app"].log
        log.webhook_url = "https://discord.com/api/webhooks/123/token"
        log.thread_id = "123"
        log.subscription_id = "sub"
        log.live_enabled = True

    await store.mutate(enable_live)
    with pytest.raises(ValueError, match="stop live logs"):
        await service.edit_app(name="app", channel_id=1234, log_identifier="new-id")


@pytest.mark.asyncio
async def test_edit_commands_reject_empty_mutations(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    await store.save(store.load_or_default())
    service = AdminConfigService(store)
    await service.add_server(name="local", server_type="local", compose_argv=("docker", "compose"))
    await service.add_stack(name="bots", server="local", channel_id=1234)

    with pytest.raises(ValueError, match="at least one server field"):
        await service.edit_server(name="local")
    with pytest.raises(ValueError, match="at least one stack field"):
        await service.edit_stack(channel_id=1234)
