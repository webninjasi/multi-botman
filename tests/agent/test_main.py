import asyncio

import pytest

from botman_agent.config import AgentApp, AgentConfig, AgentSettings
import botman_agent.main as mainmod


@pytest.mark.asyncio
async def test_run_agent_starts_only_enabled_apps_and_shares_no_queue(monkeypatch, tmp_path):
    started = []
    gate = asyncio.Event()

    async def fake_run(name, app, settings, *, state_store, sink):
        started.append((name, app.identifier, state_store.state_dir))
        if len(started) == 2:
            gate.set()
        await gate.wait()

    async def fake_supervise(name, app, settings, *, runner, sleep=asyncio.sleep):
        await runner()

    monkeypatch.setattr(mainmod, "run_live_app", fake_run)
    monkeypatch.setattr(mainmod, "supervise_live_app", fake_supervise)
    config = AgentConfig(
        settings=AgentSettings(state_dir=tmp_path),
        apps={
            "app-a": AgentApp(
                identifier="tag-a",
                enabled=True,
                webhook_url="https://discord.example/a",
                thread_id="1",
                subscription_id="sa",
            ),
            "app-b": AgentApp(identifier="tag-b", enabled=False),
            "app-c": AgentApp(
                identifier="tag-c",
                enabled=True,
                webhook_url="https://discord.example/c",
                thread_id="3",
                subscription_id="sc",
            ),
        },
    )
    await asyncio.wait_for(mainmod.run_agent(config, session=object()), timeout=1)
    assert sorted(row[0] for row in started) == ["app-a", "app-c"]
