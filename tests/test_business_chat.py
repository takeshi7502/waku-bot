import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pyrogram.enums import ParseMode

from waku.plugins import business_chat, version


def _message(connection_id: str = "connection-a", *, outgoing: bool = False):
    return SimpleNamespace(
        business_connection_id=connection_id,
        chat=SimpleNamespace(id=123),
        from_user=SimpleNamespace(id=123, is_bot=False),
        text="Chào Waku",
        outgoing=outgoing,
    )


@pytest.mark.asyncio
async def test_business_chat_replies_as_connected_account_and_keeps_history(monkeypatch):
    monkeypatch.setattr(business_chat.app_config, "business_chat_enabled", True)
    monkeypatch.setattr(business_chat.app_config, "agent", True)
    monkeypatch.setattr(business_chat.app_config, "agent_model", "test/model")
    cache = {}

    async def cache_get(key, default=None):
        return cache.get(key, default)

    async def cache_set(key, value, ttl=0):
        cache[key] = value

    monkeypatch.setattr(business_chat.memttlcache, "get", cache_get)
    monkeypatch.setattr(business_chat.memttlcache, "set", cache_set)
    business_chat._connection_permissions.clear()
    connection = SimpleNamespace(
        id="connection-a",
        is_enabled=True,
        rights=SimpleNamespace(can_reply=True),
        user=SimpleNamespace(id=456),
    )
    result = SimpleNamespace(output="Ừm, chào nha.", all_messages=lambda: ["turn"])
    agent = SimpleNamespace(run=AsyncMock(return_value=result))
    monkeypatch.setattr(business_chat, "_make_business_agent", lambda: agent)
    client = SimpleNamespace(
        send_message=AsyncMock(),
        get_business_connection=AsyncMock(return_value=connection),
    )

    await business_chat._reply_to_business_message(client, _message())
    await business_chat._reply_to_business_message(client, _message())

    client.send_message.assert_awaited_with(
        chat_id=123,
        text="Ừm, chào nha.",
        business_connection_id="connection-a",
        parse_mode=ParseMode.DISABLED,
    )
    assert agent.run.await_args_list[0].kwargs["message_history"] == []
    assert agent.run.await_args_list[1].kwargs["message_history"] == ["turn"]
    client.get_business_connection.assert_awaited_once_with("connection-a")


@pytest.mark.asyncio
async def test_business_chat_ignores_outgoing_or_disabled_messages(monkeypatch):
    monkeypatch.setattr(business_chat.app_config, "business_chat_enabled", True)
    monkeypatch.setattr(business_chat.app_config, "agent", True)
    monkeypatch.setattr(business_chat.app_config, "agent_model", "test/model")
    client = SimpleNamespace(send_message=AsyncMock())
    await business_chat._reply_to_business_message(client, _message(outgoing=True))
    monkeypatch.setattr(business_chat.app_config, "business_chat_enabled", False)
    await business_chat._reply_to_business_message(client, _message())
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_chat_requires_reply_permission(monkeypatch):
    monkeypatch.setattr(business_chat.app_config, "business_chat_enabled", True)
    monkeypatch.setattr(business_chat.app_config, "agent", True)
    monkeypatch.setattr(business_chat.app_config, "agent_model", "test/model")
    business_chat._connection_permissions.clear()
    connection = SimpleNamespace(
        id="connection-a",
        is_enabled=True,
        rights=SimpleNamespace(can_reply=False),
        user=SimpleNamespace(id=456),
    )
    client = SimpleNamespace(
        send_message=AsyncMock(),
        get_business_connection=AsyncMock(return_value=connection),
    )
    await business_chat._reply_to_business_message(client, _message())
    client.send_message.assert_not_awaited()

    connection.rights.can_reply = True
    await business_chat.business_connection_changed(client, connection)
    message = _message()
    message.from_user.id = 456
    await business_chat._reply_to_business_message(client, message)
    client.send_message.assert_not_awaited()


def test_business_switch_is_available_for_existing_settings(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text('agent = true\n[agent_providers.default]\nkey = "example"\n', encoding="utf-8")
    monkeypatch.setattr(version, "_SETTINGS_PATH", settings_path)

    groups, entries, _ = version._parse_settings()
    assert any(group.group_id == "business" for group in groups)
    assert entries["business_chat_enabled"].value is False

    version._write_entry(entries["business_chat_enabled"], True)
    parsed = tomllib.loads(settings_path.read_text(encoding="utf-8"))
    assert parsed["business_chat_enabled"] is True
    assert parsed["agent_providers"]["default"]["key"] == "example"

    groups, entries, _ = version._parse_settings()
    assert [group.keys for group in groups if group.group_id == "business"] == [
        ["business_chat_enabled"]
    ]
    assert entries["business_chat_enabled"].value is True
