import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pyrogram.enums import ChatType

from waku.plugins import business_chat, version
from waku.plugins.agent.output import OfficialRichDraftStreamer, StreamingOutput
from waku.plugins.agent.rich_output import send_rich_reply


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
    monkeypatch.setattr(business_chat.app_config, "agent_streaming", False)
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
    reply_output = AsyncMock()
    monkeypatch.setattr(business_chat, "reply_output", reply_output)
    client = SimpleNamespace(
        get_business_connection=AsyncMock(return_value=connection),
    )

    await business_chat._reply_to_business_message(client, _message())
    await business_chat._reply_to_business_message(client, _message())

    assert reply_output.await_count == 2
    assert reply_output.await_args.args[1].business_connection_id == "connection-a"
    assert reply_output.await_args.args[2] == "Ừm, chào nha."
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


@pytest.mark.asyncio
async def test_disabling_business_during_model_run_suppresses_reply(monkeypatch):
    monkeypatch.setattr(business_chat.app_config, "business_chat_enabled", True)
    monkeypatch.setattr(business_chat.app_config, "agent", True)
    monkeypatch.setattr(business_chat.app_config, "agent_model", "test/model")
    monkeypatch.setattr(business_chat.app_config, "agent_streaming", False)
    business_chat._connection_permissions.clear()
    connection = SimpleNamespace(
        id="connection-a",
        is_enabled=True,
        rights=SimpleNamespace(can_reply=True),
        user=SimpleNamespace(id=456),
    )

    async def finish_after_disable(*args, **kwargs):
        business_chat.app_config.business_chat_enabled = False
        return SimpleNamespace(output="A reply that must not be sent")

    monkeypatch.setattr(
        business_chat,
        "_make_business_agent",
        lambda: SimpleNamespace(run=finish_after_disable),
    )
    reply_output = AsyncMock()
    monkeypatch.setattr(business_chat, "reply_output", reply_output)
    client = SimpleNamespace(
        send_message=AsyncMock(),
        get_business_connection=AsyncMock(return_value=connection),
    )

    await business_chat._reply_to_business_message(client, _message())

    client.send_message.assert_not_awaited()
    reply_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_chat_streams_deltas_and_finalizes(monkeypatch):
    monkeypatch.setattr(business_chat.app_config, "business_chat_enabled", True)
    monkeypatch.setattr(business_chat.app_config, "agent", True)
    monkeypatch.setattr(business_chat.app_config, "agent_model", "test/model")
    monkeypatch.setattr(business_chat.app_config, "agent_streaming", True)
    business_chat._connection_permissions.clear()
    connection = SimpleNamespace(
        id="connection-a",
        is_enabled=True,
        rights=SimpleNamespace(can_reply=True),
        user=SimpleNamespace(id=456),
    )

    class StreamResult:
        async def stream_text(self, *, delta):
            assert delta is True
            for part in ("**Chào", " bạn**"):
                yield part

        async def get_output(self):
            return "**Chào bạn**"

        def all_messages(self):
            return ["turn"]

    class StreamContext:
        async def __aenter__(self):
            return StreamResult()

        async def __aexit__(self, *_):
            return False

    def run_stream(*args, **kwargs):
        return StreamContext()

    monkeypatch.setattr(
        business_chat,
        "_make_business_agent",
        lambda: SimpleNamespace(run_stream=run_stream),
    )
    outputs = []

    class FakeStreamingOutput:
        def __init__(self, client, message):
            assert message.business_connection_id == "connection-a"
            self.current_text = ""
            self.appended = []
            self.finalize = AsyncMock()
            self.abort = AsyncMock()
            outputs.append(self)

        async def append_delta(self, delta):
            self.appended.append(delta)
            self.current_text += delta

    monkeypatch.setattr(business_chat, "StreamingOutput", FakeStreamingOutput)
    cache_set = AsyncMock()
    monkeypatch.setattr(business_chat.memttlcache, "get", AsyncMock(return_value=[]))
    monkeypatch.setattr(business_chat.memttlcache, "set", cache_set)
    client = SimpleNamespace(get_business_connection=AsyncMock(return_value=connection))

    await business_chat._reply_to_business_message(client, _message())

    assert outputs[0].appended == ["**Chào", " bạn**"]
    outputs[0].finalize.assert_awaited_once()
    outputs[0].abort.assert_not_awaited()
    assert cache_set.await_args.args[1] == ["turn"]


@pytest.mark.asyncio
async def test_business_draft_and_edit_keep_connection_id():
    import waku.plugins.agent.output as agent_output

    agent_output._OFFICIAL_DRAFT_UNSUPPORTED_PEERS.clear()
    message = SimpleNamespace(
        business_connection_id="connection-a",
        chat=SimpleNamespace(id=123, type=ChatType.PRIVATE),
        message_thread_id=None,
        sender_chat=None,
        from_user=None,
        guest_query_id=None,
    )
    sent = SimpleNamespace(chat=message.chat, id=9, business_connection_id=None)
    message.reply_text = AsyncMock(return_value=sent)
    client = SimpleNamespace(
        resolve_peer=AsyncMock(return_value="peer"),
        invoke=AsyncMock(),
        edit_message_text=AsyncMock(return_value=sent),
    )
    draft = OfficialRichDraftStreamer(client, message)
    draft.update("**Chào**")
    assert await draft._send_draft()
    assert client.invoke.await_args.kwargs["business_connection_id"] == "connection-a"

    output = StreamingOutput(client, message)
    await output._send_new_message("**Chào**")
    assert sent.business_connection_id == "connection-a"
    await output._do_edit("**Chào bạn**")
    assert client.edit_message_text.await_args.kwargs["business_connection_id"] == "connection-a"


@pytest.mark.asyncio
async def test_business_rich_reply_uses_entities():
    message = SimpleNamespace(
        chat=SimpleNamespace(id=123),
        business_connection_id="connection-a",
        reply_text=AsyncMock(return_value=SimpleNamespace(id=9)),
    )
    await send_rich_reply(SimpleNamespace(), message, "**Chào bạn**")
    sent_text = message.reply_text.await_args.args[0]
    entities = message.reply_text.await_args.kwargs["entities"]
    assert sent_text == "Chào bạn"
    assert entities


@pytest.mark.asyncio
async def test_business_table_uses_rich_message_with_connection_id():
    message = SimpleNamespace(
        chat=SimpleNamespace(id=123),
        id=7,
        message_thread_id=None,
        business_connection_id="connection-a",
    )
    client = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(id=9)))

    await send_rich_reply(client, message, "| A | B |\n|---|---|\n| 1 | 2 |")

    assert client.send_rich_message.await_args.kwargs["business_connection_id"] == "connection-a"
    rich_message = client.send_rich_message.await_args.kwargs["rich_message"]
    assert "| A | B |" in rich_message.markdown


@pytest.mark.asyncio
async def test_business_draft_failure_falls_back_to_message_edit():
    import waku.plugins.agent.output as agent_output

    agent_output._OFFICIAL_DRAFT_UNSUPPORTED_PEERS.clear()
    chat = SimpleNamespace(id=123, type=ChatType.PRIVATE)
    sent = SimpleNamespace(chat=chat, id=9, business_connection_id=None)
    message = SimpleNamespace(
        business_connection_id="connection-a",
        chat=chat,
        message_thread_id=None,
        sender_chat=None,
        from_user=None,
        guest_query_id=None,
        reply_text=AsyncMock(return_value=sent),
    )
    client = SimpleNamespace(
        resolve_peer=AsyncMock(return_value="peer"),
        invoke=AsyncMock(side_effect=RuntimeError("draft unavailable")),
        edit_message_text=AsyncMock(return_value=sent),
    )
    output = StreamingOutput(client, message)

    await output.append_delta("**Chào")
    await output.append_delta(" bạn**")
    await output.finalize()

    message.reply_text.assert_awaited_once()
    client.edit_message_text.assert_awaited()
    assert client.edit_message_text.await_args.kwargs["business_connection_id"] == "connection-a"


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


def test_business_switch_takes_effect_without_reload(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text('agent = true\n', encoding="utf-8")
    monkeypatch.setattr(version, "_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(version.app_config, "business_chat_enabled", False)
    version._DIRTY_KEYS.clear()

    _, entries, _ = version._parse_settings()
    assert version._toggle_entry(entries["business_chat_enabled"], 123) is True
    assert business_chat.app_config.business_chat_enabled is True
    assert not version._is_dirty(123)
    assert tomllib.loads(settings_path.read_text(encoding="utf-8"))["business_chat_enabled"] is True

    _, entries, _ = version._parse_settings()
    assert version._toggle_entry(entries["business_chat_enabled"], 123) is True
    assert business_chat.app_config.business_chat_enabled is False
    assert not version._is_dirty(123)
