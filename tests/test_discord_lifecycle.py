import asyncio
from types import SimpleNamespace

import pytest

from waku.discordbot import runtime, state


def test_discord_dm_uses_personal_prompt_and_guild_uses_group_prompt(monkeypatch):
    monkeypatch.setattr(runtime.app_config, "agent_prompt", "DM persona")
    monkeypatch.setattr(runtime.app_config, "agent_group_prompt", "Group persona")

    assert runtime._discord_persona_prompt(SimpleNamespace(guild=None)) == "DM persona"
    assert runtime._discord_persona_prompt(SimpleNamespace(guild=object())) == "Group persona"


@pytest.mark.asyncio
async def test_discord_start_is_idempotent_and_stop_resets_runtime(monkeypatch):
    stopped = asyncio.Event()
    clients = []

    class FakeClient:
        user = None

        async def start(self, token):
            assert token == "test-token"
            await stopped.wait()

        async def close(self):
            stopped.set()

        def is_ready(self):
            return False

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def instructions(self, callback):
            return callback

    def create_client():
        client = FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(runtime.app_config, "discord_enabled", True)
    monkeypatch.setattr(runtime.app_config, "discord_token", "test-token")
    monkeypatch.setattr(runtime.app_config, "agent", True)
    monkeypatch.setattr(runtime.app_config, "agent_model", "test/model")
    monkeypatch.setattr(runtime.provider, "make_chat_model", lambda _: "model")
    monkeypatch.setattr(runtime, "Agent", FakeAgent)
    monkeypatch.setattr(runtime, "Tool", lambda func, **kwargs: func)
    monkeypatch.setattr(runtime, "_create_client", create_client)
    monkeypatch.setattr(state, "discord_task", None)
    monkeypatch.setattr(state, "discord_client", None)

    try:
        await runtime.start_discord_bot()
        first_task = state.discord_task
        await runtime.start_discord_bot()
        assert len(clients) == 1
        assert state.discord_task is first_task

        state.server_list_view_registered = True
        await runtime.stop_discord_bot()
        assert state.discord_task is None
        assert state.discord_client is None
        assert state.discord_agent is None
        assert state.discord_recovery_agent is None
        assert state.server_list_view_registered is False
    finally:
        if state.discord_task is not None or state.discord_client is not None:
            await runtime.stop_discord_bot()


@pytest.mark.asyncio
async def test_discord_stop_cleans_up_after_client_close_error(monkeypatch):
    class FakeClient:
        async def close(self):
            raise RuntimeError("connection already closed")

    task = asyncio.create_task(asyncio.sleep(0))
    await task
    monkeypatch.setattr(state, "discord_client", FakeClient())
    monkeypatch.setattr(state, "discord_task", task)
    monkeypatch.setattr(state, "discord_agent", SimpleNamespace())
    monkeypatch.setattr(state, "discord_recovery_agent", SimpleNamespace())

    await runtime.stop_discord_bot()

    assert state.discord_client is None
    assert state.discord_task is None
    assert state.discord_agent is None
    assert state.discord_recovery_agent is None
