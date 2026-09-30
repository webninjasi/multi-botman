from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from botman.config import ConfigError, ConfigStore
from botman.models import BotmanConfig, LocalServerConfig, SSHServerConfig
from botman.routing import ChannelAuthorizationError, authorize_app_channel


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
            }
        },
        "apps": {
            "app-a": {
                "stack": "bots",
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


def test_valid_config_and_derived_paths() -> None:
    config = BotmanConfig.model_validate(valid_config_dict())
    assert isinstance(config.servers["local"], LocalServerConfig)
    assert isinstance(config.servers["remote-1"], SSHServerConfig)
    assert str(config.stack_root("bots")) == "/srv/botman/stacks/bots"
    assert str(config.compose_path("bots")) == "/srv/botman/stacks/bots/compose.yml"
    assert str(config.app_release_root("app-a")) == "/srv/botman/stacks/bots/apps/app-a"
    assert str(config.app_env_path("app-a")) == "/srv/botman/stacks/bots/env/app-a.env"
    assert list(config.apps_for_server("remote-1")) == ["app-a"]
    assert list(config.apps_for_channel(1234567890)) == ["app-a"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("servers", "bad/name"), {"type": "local", "compose_argv": ["docker", "compose"]}),
        (("stacks", "bots", "channel_id"), "not-numeric"),
        (("stacks", "bots", "project_name"), "../../bad"),
        (("apps", "app-a", "service"), "app;rm"),
        (("apps", "app-a", "env_file"), "../secret"),
        (("apps", "app-a", "log_identifier"), "bad identifier"),
        (("apps", "app-a", "git", "branch"), "../main"),
        (("settings", "timezone"), "Not/A_Zone"),
        (("settings", "release_keep_count"), 0),
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
    raw["apps"]["app-a"]["surprise"] = True
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        BotmanConfig.model_validate(raw)


def test_unknown_references_rejected() -> None:
    raw = valid_config_dict()
    raw["stacks"]["bots"]["server"] = "missing"
    with pytest.raises(ValueError, match="unknown server"):
        BotmanConfig.model_validate(raw)

    raw = valid_config_dict()
    raw["apps"]["app-a"]["stack"] = "missing"
    with pytest.raises(ValueError, match="unknown stack"):
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


def test_channel_routing_rejects_wrong_channel() -> None:
    config = BotmanConfig.model_validate(valid_config_dict())
    resolved = authorize_app_channel(config, "app-a", "1234567890")
    assert resolved.stack_name == "bots"
    assert resolved.server_name == "remote-1"

    with pytest.raises(ChannelAuthorizationError):
        authorize_app_channel(config, "app-a", "999999")


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
    raw["apps"]["app-a"]["git"]["repo_url"] = "https://github.com/owner/app-a.git"
    with pytest.raises(ValueError, match="must use SSH"):
        BotmanConfig.model_validate(raw)
