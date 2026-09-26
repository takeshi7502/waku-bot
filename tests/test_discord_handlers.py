import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from waku.discordbot import handlers, state
from waku.discordbot.agent import DiscordPostRunError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_error", "expected_reply"),
    [
        (TimeoutError("model timed out"), "Thử lại sau nhé"),
        (DiscordPostRunError("reply failed"), None),
    ],
)
async def test_discord_does_not_retry_failed_or_completed_model_turns(
    monkeypatch, run_error, expected_reply
):
    channel = SimpleNamespace(id=123, name="test", send=AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(id=987),
        guild=None,
        channel=channel,
    )
    monkeypatch.setattr(state, "discord_agent", object())
    monkeypatch.setattr(handlers.app_config, "agent_periodic_reaction_interval", 0)
    monkeypatch.setattr(handlers, "_history_key", AsyncMock(return_value="discord:test:987"))
    monkeypatch.setattr(handlers, "_build_prompt", AsyncMock(return_value=(["prompt"], False)))
    monkeypatch.setattr(handlers.common.memttlcache, "get", AsyncMock(return_value=[]))
    monkeypatch.setattr(handlers, "_is_discord_history_error", lambda error: False)
    run_once = AsyncMock(side_effect=run_error)
    recovery = AsyncMock(return_value="Thử lại sau nhé")
    monkeypatch.setattr(handlers, "_run_discord_agent_once", run_once)
    monkeypatch.setattr(handlers, "_discord_recovery_reply", recovery)
    waiting_key = handlers._waiting_key(message.author.id)

    try:
        await handlers._handle_message(message, "hello")
    finally:
        await handlers.common.memstore.delete(waiting_key)

    run_once.assert_awaited_once()
    if expected_reply is None:
        recovery.assert_not_awaited()
        channel.send.assert_not_awaited()
    else:
        recovery.assert_awaited_once()
        channel.send.assert_awaited_once_with(expected_reply, reference=message)


@pytest.mark.asyncio
async def test_discord_serializes_same_user_before_prompt_preparation(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    channel = SimpleNamespace(id=123, name="test", send=AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(id=988), guild=None, channel=channel
    )

    async def slow_prompt(*args):
        entered.set()
        await release.wait()
        return (["prompt"], False)

    monkeypatch.setattr(state, "discord_agent", object())
    monkeypatch.setattr(handlers.app_config, "agent_periodic_reaction_interval", 0)
    monkeypatch.setattr(handlers, "_history_key", AsyncMock(return_value="discord:test:988"))
    monkeypatch.setattr(handlers, "_build_prompt", slow_prompt)
    monkeypatch.setattr(handlers.common.memttlcache, "get", AsyncMock(return_value=[]))
    run_once = AsyncMock()
    monkeypatch.setattr(handlers, "_run_discord_agent_once", run_once)
    waiting_key = handlers._waiting_key(message.author.id)

    first_turn = asyncio.create_task(handlers._handle_message(message, "first"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await handlers._handle_message(message, "second")
        channel.send.assert_awaited_once_with("Thinking...", reference=message)
        assert run_once.await_count == 0
        release.set()
        await asyncio.wait_for(first_turn, timeout=1)
        run_once.assert_awaited_once()
    finally:
        release.set()
        if not first_turn.done():
            first_turn.cancel()
            try:
                await first_turn
            except asyncio.CancelledError:
                pass
        await handlers.common.memstore.delete(waiting_key)
