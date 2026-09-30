"""Central control of per-target Botman log-agent configuration and service state."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import yaml

from botman_agent.config import AgentApp, AgentConfig, AgentSettings

from .compose import executor_for_server
from .executor import ExecResult
from .models import BotmanConfig

AGENT_CONFIG_PATH = PurePosixPath("/var/lib/botman-log-agent/config.yaml")
AGENT_SERVICE = "botman-log-agent.service"
AGENT_USER = "botman-log-agent"


class AgentControlError(RuntimeError):
    pass


def render_server_agent_config(config: BotmanConfig, server_name: str) -> bytes:
    if server_name not in config.servers:
        raise KeyError(f"unknown server: {server_name}")
    apps: dict[str, AgentApp] = {}
    for stack_name, stack in config.stacks.items():
        if stack.server != server_name:
            continue
        for app_name, app in stack.apps.items():
            agent_key = config.agent_app_key(stack_name, app_name)
            log = app.log
            if log.live_enabled:
                apps[agent_key] = AgentApp(
                    identifier=app.log_identifier,
                    enabled=True,
                    webhook_url=log.webhook_url,
                    thread_id=log.thread_id,
                    subscription_id=log.subscription_id,
                )
            else:
                apps[agent_key] = AgentApp(identifier=app.log_identifier, enabled=False)
    agent_config = AgentConfig(
        settings=AgentSettings(
            state_dir=Path("/var/lib/botman-log-agent/state"),
            resume_max_age_sec=config.settings.live_resume_max_age_sec,
        ),
        apps=apps,
    )
    payload = yaml.safe_dump(
        agent_config.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )
    return payload.encode("utf-8")


class AgentControlService:
    def __init__(self, config: BotmanConfig):
        self.config = config

    async def sync(self, server_name: str, *, restart: bool = True) -> ExecResult | None:
        try:
            server = self.config.servers[server_name]
        except KeyError as exc:
            raise KeyError(f"unknown server: {server_name}") from exc
        executor = executor_for_server(server)
        payload = render_server_agent_config(self.config, server_name)
        writer = getattr(executor, "write_bytes", None)
        if not callable(writer):
            raise AgentControlError("target executor cannot write agent config")
        # Provisioning creates a setgid botman-log-agent directory and adds the
        # management identity to that group. This avoids granting broad sudo
        # file-copy/move rights merely to refresh a webhook configuration.
        await writer(AGENT_CONFIG_PATH, payload, mode=0o640, atomic=True)
        if not restart:
            return None
        await executor.run(["sudo", "-n", "systemctl", "restart", AGENT_SERVICE], timeout=30, check=True)
        result = await executor.run(
            ["sudo", "-n", "systemctl", "is-active", AGENT_SERVICE], timeout=15, check=False
        )
        if not result.ok or result.stdout.strip() != "active":
            raise AgentControlError(
                f"{AGENT_SERVICE} did not become active: "
                f"{(result.stdout or result.stderr).strip() or f'exit {result.returncode}'}"
            )
        return result

    async def status(self, server_name: str) -> ExecResult:
        try:
            server = self.config.servers[server_name]
        except KeyError as exc:
            raise KeyError(f"unknown server: {server_name}") from exc
        executor = executor_for_server(server)
        return await executor.run(
            ["sudo", "-n", "systemctl", "status", "--no-pager", "--lines=20", AGENT_SERVICE],
            timeout=20,
            check=False,
        )
