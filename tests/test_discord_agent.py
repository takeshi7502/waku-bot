import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from waku import common
from waku.config import app_config
from waku.discordbot import agent, messages, state


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _message():
    return SimpleNamespace(channel=SimpleNamespace(typing=lambda: _Typing()))


def test_discord_history_error_does_not_classify_unrelated_400():
    error = RuntimeError("Thinking mode does not support this tool_choice")
    error.status_code = 400
    assert not agent._is_discord_history_error(error)

    history_error = RuntimeError("messages[2].tool_call_id did not match previous message")
    history_error.status_code = 400
    assert agent._is_discord_history_error(history_error)


@pytest.mark.asyncio
async def test_discord_agent_model_run_has_deadline(monkeypatch):
    async def never_ends(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(state, "discord_agent", SimpleNamespace(run=never_ends))
    monkeypatch.setattr(app_config, "agent_run_timeout", 0.01)

    with pytest.raises(TimeoutError):
        await agent._run_discord_agent_once(_message(), ["hello"], "history", [], None)


@pytest.mark.asyncio
async def test_discord_delivery_failure_does_not_store_history_or_retry_model(monkeypatch):
    model_run = AsyncMock(
        return_value=SimpleNamespace(output="answer", all_messages=lambda: [])
    )
    cache_set = AsyncMock()
    monkeypatch.setattr(state, "discord_agent", SimpleNamespace(run=model_run))
    monkeypatch.setattr(messages, "_send_reply", AsyncMock(side_effect=RuntimeError("send failed")))
    monkeypatch.setattr(agent, "_send_reply", messages._send_reply)
    monkeypatch.setattr(common.memttlcache, "set", cache_set)

    with pytest.raises(agent.DiscordPostRunError, match="delivery"):
        await agent._run_discord_agent_once(_message(), ["hello"], "history", [], None)

    model_run.assert_awaited_once()
    cache_set.assert_not_awaited()


def test_discord_reply_splits_long_paragraph_without_silent_loss():
    text = "a" * 5000
    chunks = messages._split_reply(text)

    assert len(chunks) == 3
    assert all(0 < len(chunk) <= 1900 for chunk in chunks)
    assert "".join(chunks) == text


def test_discord_prompt_time_uses_configured_timezone(monkeypatch):
    monkeypatch.setattr(app_config, "timezone", "Asia/Ho_Chi_Minh")
    assert messages._discord_current_time().endswith("+07:00")


def test_discord_reply_preserves_code_fences_across_chunks():
    text = "```python\n" + "\n".join("x = 1" for _ in range(500)) + "\n```"
    chunks = messages._split_reply(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)


def test_discord_reply_marks_oversized_output_as_truncated():
    chunks = messages._split_reply("x" * 20000)

    assert len(chunks) == messages.DISCORD_REPLY_MAX_MESSAGES
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert "đã lược bớt" in chunks[-1]


@pytest.mark.asyncio
async def test_discord_prompt_media_timeout_falls_back_to_text(monkeypatch):
    async def never_ends(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(messages, "_build_prompt_impl", never_ends)
    monkeypatch.setattr(messages, "_guild_name", lambda _message: "Server")
    monkeypatch.setattr(messages, "_channel_name", lambda _message: "general")
    monkeypatch.setattr(messages, "_author_name", lambda _message: "User")
    monkeypatch.setattr(app_config, "agent_download_timeout", 0.01)
    message = SimpleNamespace(
        author=SimpleNamespace(id=7), channel=SimpleNamespace(id=8)
    )

    prompt, needs_multimodal = await messages._build_prompt(message, "Mô tả ảnh này")

    assert needs_multimodal is False
    assert "Mô tả ảnh này" in prompt[0]
    assert "attachments and stickers are unavailable" in prompt[0]


@pytest.mark.asyncio
async def test_discord_reply_does_not_allow_everyone_or_role_pings():
    send = AsyncMock()
    message = SimpleNamespace(channel=SimpleNamespace(send=send))

    await messages._send_reply(message, "hello @everyone <@&123456789012345678>")

    kwargs = send.await_args.kwargs
    assert not kwargs["allowed_mentions"].everyone
    assert not kwargs["allowed_mentions"].roles
    assert not kwargs["allowed_mentions"].replied_user


@pytest.mark.asyncio
async def test_discord_channel_resolution_rejects_other_guild(monkeypatch):
    source_guild = SimpleNamespace(
        id=1,
        get_channel_or_thread=lambda _id: None,
        get_channel=lambda _id: None,
        get_thread=lambda _id: None,
    )
    other_guild_channel = SimpleNamespace(id=99, guild=SimpleNamespace(id=2))
    client = SimpleNamespace(
        get_channel=Mock(return_value=other_guild_channel),
        fetch_channel=AsyncMock(return_value=other_guild_channel),
    )
    monkeypatch.setattr(state, "discord_client", client)
    message = SimpleNamespace(guild=source_guild, channel=SimpleNamespace(id=10))

    assert await messages._resolve_discord_channel(message, 99) is None
    client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_dm_channel_resolution_cannot_target_arbitrary_channel(monkeypatch):
    client = SimpleNamespace(get_channel=Mock(), fetch_channel=AsyncMock())
    monkeypatch.setattr(state, "discord_client", client)
    current_channel = SimpleNamespace(id=10)
    message = SimpleNamespace(guild=None, channel=current_channel)

    assert await messages._resolve_discord_channel(message, 10) is current_channel
    assert await messages._resolve_discord_channel(message, 99) is None
    client.get_channel.assert_not_called()
    client.fetch_channel.assert_not_awaited()
