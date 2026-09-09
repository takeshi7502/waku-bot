import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pyrogram.enums import ChatAction

from waku.config import app_config
from waku.plugins.agent import runner
from waku.plugins.agent.output import StreamingOutput, TypingKeepAlive


@pytest.mark.asyncio
async def test_agent_run_timeout_cancels_turn_and_notifies_user(monkeypatch):
    cancelled = asyncio.Event()

    async def stuck_agent_run(**_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runner, "_run_agent_impl", stuck_agent_run)
    monkeypatch.setattr(app_config, "agent_run_timeout", 0.01)
    message = SimpleNamespace(reply_text=AsyncMock())

    await runner.run_agent(
        agi=None,
        client=AsyncMock(),
        message=message,
        user_id=123,
        chat_id=456,
        user_prompt=[],
        history=[],
        deps=SimpleNamespace(is_guest_mode=False),
        multimodal_model=None,
        model=None,
        lang="vi-VN",
    )

    assert cancelled.is_set()
    message.reply_text.assert_awaited_once()
    assert "quá nhiều thời gian" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_typing_keep_alive_sends_cancel_when_stopped():
    client = SimpleNamespace(send_chat_action=AsyncMock())
    message = SimpleNamespace(chat=SimpleNamespace(id=456))
    typing = TypingKeepAlive(client, message)

    typing.start()
    await asyncio.sleep(0)
    await typing.stop()

    actions = [call.kwargs["action"] for call in client.send_chat_action.await_args_list]
    assert ChatAction.TYPING in actions
    assert actions[-1] == ChatAction.CANCEL


@pytest.mark.asyncio
async def test_streaming_abort_stops_official_draft_task():
    message = SimpleNamespace(
        chat=None,
        sender_chat=None,
        from_user=None,
        guest_query_id=None,
    )
    output = StreamingOutput(AsyncMock(), message)
    output.official_draft.stop = AsyncMock()

    await output.abort()

    output.official_draft.stop.assert_awaited_once()
