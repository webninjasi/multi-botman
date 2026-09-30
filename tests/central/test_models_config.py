from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from botman.config import ConfigError, ConfigStore
from botman.models import BotmanConfig, LocalServerConfig, SSHServerConfig
from botman.routing import ChannelAuthorizationError, UnknownAppError, authorize_app_channel


def valid_config_dict() -> dict:
    return {
        "settings": {"timezone": "Europe/Istanbul"},
        "servers": {
            "local": {"type": "local", "compose_argv": ["sudo", "podman", "compose"]},
            "remote-1": {
                "type": "ssh",
                "host": "10.0.0.2",
                "port": 22,
                "user": "botmgr",
                "key": "/home/botman/.ssh/server-remote-1",
                "known_hosts": "/home/botman/.ssh/known_hosts",
                "compose_argv": ["sudo", "docker", "compose"],
            },
        },
        "stacks": {
            "bots": {
                "server": "remote-1",
                "channel_id": "1234567890",
                "project_name": "botman-bots",
                "compose_file": "compose.yml",
                "apps": {
                    "app-a": {
                        "service": "app-a",
                        "log_identifier": "botman-bots-app-a",
                        "git": {
                            "repo_url": "git@github.com:owner/app-a.git",
                            "branch": "main",
                            "deploy_key_path": "/home/botman/.ssh/deploy-app-a",
                        },
                    }
                },
            }
        },
    }


def test_valid_config_and_derived_paths() -> None:
    config = BotmanConfig.model_validate(valid_config_dict())
    assert isinstance(config.servers["local"], LocalServerConfig)
    assert isinstance(config.servers["remote-1"], SSHServerConfig)
    assert str(config.stack_root("bots")) == "/srv/botman/stacks/bots"
    assert str(config.compose_path("bots")) == "/srv/botman/stacks/bots/compose.yml"
    assert str(config.app_release_root("bots", "app-a")) == "/srv/botman/stacks/bots/apps/app-a"
    assert str(config.app_env_path("bots", "app-a")) == "/srv/botman/stacks/bots/env/app-a.env"
    assert list(config.apps_for_server("remote-1")) == ["bots.app-a"]
    assert list(config.apps_for_channel(1234567890)) == ["app-a"]
    assert str(config.settings.log_export_bin) == "/opt/botman-agent-venv/bin/botman-log-export"
    assert str(config.settings.agent_config_path) == "/var/lib/botman-log-agent/config.yaml"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("servers", "bad/name"), {"type": "local", "compose_argv": ["docker", "compose"]}),
        (("stacks", "bots", "channel_id"), "not-numeric"),
        (("stacks", "bots", "project_name"), "../../bad"),
        (("stacks", "bots", "apps", "app-a", "service"), "app;rm"),
        (("stacks", "bots", "apps", "app-a", "env_file"), "../secret"),
        (("stacks", "bots", "apps", "app-a", "log_identifier"), "bad identifier"),
        (("stacks", "bots", "apps", "app-a", "git", "branch"), "../main"),
        (("settings", "timezone"), "Not/A_Zone"),
        (("settings", "release_keep_count"), 0),
        (("settings", "log_export_bin"), "relative/exporter"),
        (("settings", "agent_config_path"), "relative/config.yaml"),
    ],
)
def test_invalid_values_are_rejected(path: tuple[str, ...], value) -> None:
    raw = valid_config_dict()
    cursor = raw
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ValueError):
        BotmanConfig.model_validate(raw)


def test_invalid_ssh_port_and_relative_key_are_rejected() -> None:
    raw = valid_config_dict()
    raw["servers"]["remote-1"]["port"] = 0
    with pytest.raises(ValueError):
        BotmanConfig.model_validate(raw)

    raw = valid_config_dict()
    raw["servers"]["remote-1"]["key"] = "relative-key"
    with pytest.raises(ValueError):
        BotmanConfig.model_validate(raw)


def test_unknown_keys_rejected() -> None:
    raw = valid_config_dict()
    raw["stacks"]["bots"]["apps"]["app-a"]["surprise"] = True
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        BotmanConfig.model_validate(raw)


def test_unknown_server_reference_rejected() -> None:
    raw = valid_config_dict()
    raw["stacks"]["bots"]["server"] = "missing"
    with pytest.raises(ValueError, match="unknown server"):
        BotmanConfig.model_validate(raw)


def test_duplicate_stack_channel_rejected() -> None:
    raw = valid_config_dict()
    raw["stacks"]["other"] = {
        "server": "local",
        "channel_id": "1234567890",
        "project_name": "other",
    }
    with pytest.raises(ValueError, match="assigned to both"):
        BotmanConfig.model_validate(raw)


def test_same_app_names_are_allowed_in_different_stacks_and_channel_selects_stack() -> None:
    raw = valid_config_dict()
    raw["stacks"]["other"] = {
        "server": "local",
        "channel_id": "222",
        "project_name": "other",
        "apps": {
            "app": {
                "service": "app",
                "log_identifier": "other-app",
                "git": {"repo_url": "git@example.com:other/app.git"},
            },
            "db": {
                "service": "db",
                "log_identifier": "other-db",
                "git": {"repo_url": "git@example.com:other/db.git"},
            },
        },
    }
    raw["stacks"]["bots"]["apps"] = {
        "app": {
            "service": "app",
            "log_identifier": "bots-app",
            "git": {"repo_url": "git@example.com:bots/app.git"},
        },
        "db": {
            "service": "db",
            "log_identifier": "bots-db",
            "git": {"repo_url": "git@example.com:bots/db.git"},
        },
    }
    config = BotmanConfig.model_validate(raw)

    assert authorize_app_channel(config, "app", 1234567890).stack_name == "bots"
    assert authorize_app_channel(config, "app", 222).stack_name == "other"
    assert authorize_app_channel(config, "db", 1234567890).app.log_identifier == "bots-db"
    assert authorize_app_channel(config, "db", 222).app.log_identifier == "other-db"


def test_channel_routing_rejects_unconfigured_channel_and_unknown_local_app() -> None:
    config = BotmanConfig.model_validate(valid_config_dict())
    resolved = authorize_app_channel(config, "app-a", "1234567890")
    assert resolved.stack_name == "bots"
    assert resolved.server_name == "remote-1"

    with pytest.raises(ChannelAuthorizationError):
        authorize_app_channel(config, "app-a", "999999")
    with pytest.raises(UnknownAppError):
        authorize_app_channel(config, "missing", "1234567890")


def test_legacy_flat_apps_are_migrated_to_nested_stack_schema() -> None:
    raw = valid_config_dict()
    app = raw["stacks"]["bots"]["apps"].pop("app-a")
    raw["apps"] = {"app-a": {"stack": "bots", **app}}
    config = BotmanConfig.model_validate(raw)
    assert "app-a" in config.stacks["bots"].apps
    assert "apps" not in config.model_dump(mode="python")


def test_malformed_yaml_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("settings: [unterminated", encoding="utf-8")
    with pytest.raises(ConfigError, match="failed to read/parse"):
        ConfigStore(path).load()


@pytest.mark.asyncio
async def test_atomic_save_permissions_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "private" / "config.yaml"
    store = ConfigStore(path)
    config = BotmanConfig.model_validate(valid_config_dict())
    await store.save(config)

    assert path.exists()
    assert os.stat(path).st_mode & 0o777 == 0o600
    loaded = store.load()
    assert loaded == config
    assert not list(path.parent.glob(".config.yaml.*.tmp"))


@pytest.mark.asyncio
async def test_mutation_lock_prevents_stale_last_writer_race(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    await store.save(BotmanConfig())

    async def add_server(name: str, delay: float) -> None:
        async def mutate(config: BotmanConfig) -> None:
            await asyncio.sleep(delay)
            config.servers[name] = LocalServerConfig(type="local", compose_argv=("docker", "compose"))

        await store.mutate(mutate)

    await asyncio.gather(add_server("one", 0.03), add_server("two", 0.0))
    loaded = store.load()
    assert set(loaded.servers) == {"one", "two"}


def test_git_repo_url_must_use_ssh() -> None:
    raw = valid_config_dict()
    raw["stacks"]["bots"]["apps"]["app-a"]["git"]["repo_url"] = "https://github.com/owner/app-a.git"
    with pytest.raises(ValueError, match="must use SSH"):
        BotmanConfig.model_validate(raw)


def test_duplicate_compose_project_on_same_server_is_rejected() -> None:
    raw = valid_config_dict()
    raw["stacks"]["other"] = {
        "server": "remote-1",
        "channel_id": "222",
        "project_name": "botman-bots",
    }
    with pytest.raises(ValueError, match="project_name.*assigned to both"):
        BotmanConfig.model_validate(raw)


def test_same_compose_project_name_on_different_servers_is_allowed() -> None:
    raw = valid_config_dict()
    raw["stacks"]["other"] = {
        "server": "local",
        "channel_id": "222",
        "project_name": "botman-bots",
    }
    config = BotmanConfig.model_validate(raw)
    assert config.stacks["bots"].project_name == config.stacks["other"].project_name


def test_duplicate_compose_service_within_stack_is_rejected() -> None:
    raw = valid_config_dict()
    raw["stacks"]["bots"]["apps"]["app-b"] = {
        "service": "app-a",
        "log_identifier": "botman-bots-app-b",
        "git": {"repo_url": "git@example.com:owner/app-b.git"},
    }
    with pytest.raises(ValueError, match="Compose service.*assigned to both"):
        BotmanConfig.model_validate(raw)
