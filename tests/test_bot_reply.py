from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pyrogram.enums import ChatType

from waku import resources
from waku.database.models import ChatConfig
from waku.plugins import simple_reply
from waku.plugins.chatconfig import ChatConfigMarkup


def test_bot_reply_defaults_to_disabled_for_new_and_existing_configs():
    assert ChatConfig().bot_reply is False
    assert ChatConfig.from_dict({"ai_reply": False}).bot_reply is False
    assert ChatConfig.from_dict({"bot_reply": True}).bot_reply is True


def test_config_menu_shows_bot_reply_as_disabled_by_default():
    markup = ChatConfigMarkup(ChatConfig(), "vi-VN").build()
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert any("Bot trả lời mặc định" in label and "❌" in label for label in labels)


@pytest.mark.asyncio
async def test_disabled_bot_reply_is_silent(monkeypatch):
    reply = AsyncMock()
    monkeypatch.setattr(simple_reply, "word_reply", reply)
    message = SimpleNamespace(chat=SimpleNamespace(type=ChatType.SUPERGROUP))

    await simple_reply.bot_reply_if_enabled(
        SimpleNamespace(), message, ChatConfig(bot_reply=False)
    )

    reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_bot_reply_uses_legacy_reply(monkeypatch):
    reply = AsyncMock()
    monkeypatch.setattr(simple_reply, "word_reply", reply)
    client = SimpleNamespace()
    message = SimpleNamespace(chat=SimpleNamespace(type=ChatType.GROUP))

    await simple_reply.bot_reply_if_enabled(
        client, message, ChatConfig(ai_reply=False, bot_reply=True)
    )

    reply.assert_awaited_once_with(client, message)


def test_word_reply_dictionary_is_localized():
    resources._word_dict_cache.clear()
    vi_words = resources.get_word_dict("vi-VN")
    zh_words = resources.get_word_dict("zh-CN")
    en_words = resources.get_word_dict("en")

    assert "xin chào" in vi_words
    assert "你好" in zh_words
    assert en_words == {}
