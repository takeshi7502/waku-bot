from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from waku import common
from waku.discordbot.models import DiscordGuildSettings
from waku.discordbot import permissions, settings, state
from waku.discordbot.views import config as config_views
from waku.discordbot.views import server_list


def _interaction(*, user_id: int, guild=None, administrator: bool = False):
    return SimpleNamespace(
        guild=guild,
        user=SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(administrator=administrator),
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_guild_config_buttons_require_current_admin_permission(monkeypatch):
    guild = SimpleNamespace(id=123, owner_id=1)
    view = config_views.DiscordConfigView(guild, DiscordGuildSettings(enabled=True))
    monkeypatch.setattr(
        config_views, "_discord_guild_settings", AsyncMock(return_value=DiscordGuildSettings(enabled=True))
    )
    monkeypatch.setattr(config_views, "_is_discord_user_bot_admin", lambda user: user.id == 9)

    ordinary = _interaction(user_id=2, guild=guild)
    assert not await view.interaction_check(ordinary)
    ordinary.response.send_message.assert_awaited_once()

    owner = _interaction(user_id=1, guild=guild)
    assert await view.interaction_check(owner)

    administrator = _interaction(user_id=3, guild=guild, administrator=True)
    assert await view.interaction_check(administrator)

    bot_admin = _interaction(user_id=9, guild=guild)
    assert await view.interaction_check(bot_admin)


@pytest.mark.asyncio
async def test_guild_config_rejects_wrong_or_revoked_server(monkeypatch):
    guild = SimpleNamespace(id=123, owner_id=1)
    view = config_views.DiscordConfigView(guild, DiscordGuildSettings(enabled=True))
    monkeypatch.setattr(config_views, "_is_discord_user_bot_admin", lambda user: False)

    wrong_guild = _interaction(user_id=1, guild=SimpleNamespace(id=456, owner_id=1))
    assert not await view.interaction_check(wrong_guild)

    monkeypatch.setattr(
        config_views, "_discord_guild_settings", AsyncMock(return_value=DiscordGuildSettings(enabled=False))
    )
    revoked = _interaction(user_id=1, guild=guild)
    assert not await view.interaction_check(revoked)
    revoked.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_dm_config_buttons_only_accept_owner():
    user = SimpleNamespace(id=123)
    view = config_views.DiscordDMConfigView(user, DiscordGuildSettings(enabled=True))

    intruder = _interaction(user_id=456)
    assert not await view.interaction_check(intruder)
    intruder.response.send_message.assert_awaited_once()

    owner = _interaction(user_id=123)
    assert await view.interaction_check(owner)


@pytest.mark.asyncio
async def test_dm_settings_read_error_does_not_enable_ai(monkeypatch):
    from waku.database import db

    monkeypatch.setattr(db, "AsyncSessionFactory", Mock(side_effect=RuntimeError("offline")))
    result = await settings._discord_dm_settings(SimpleNamespace(id=123))
    assert not result.ai_reply


@pytest.mark.asyncio
async def test_guild_settings_cache_returns_independent_copy(monkeypatch):
    cached = DiscordGuildSettings(enabled=False)
    monkeypatch.setattr(common.memttlcache, "get", AsyncMock(return_value=cached))

    result = await settings._discord_guild_settings(SimpleNamespace(id=123, name="test"))
    result.enabled = True

    assert cached.enabled is False


@pytest.mark.asyncio
async def test_unauthorizing_discord_preserves_shared_chat_data(monkeypatch):
    from waku.database import db
    from waku.database.models import ChatConfig, ChatData

    chat = ChatData(id=123, title="test server", username=None)
    chat.config = ChatConfig(
        discord_enabled=True,
        discord_auth_status="pending",
        discord_auth_requester_id=456,
        greeting="keep me",
    ).to_dict()
    session = SimpleNamespace(
        get=AsyncMock(return_value=chat),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(db, "AsyncSessionFactory", lambda: context)
    monkeypatch.setattr(common.memttlcache, "delete", AsyncMock())

    await settings._delete_discord_guild_settings_by_id(chat.id)

    session.delete.assert_not_awaited()
    session.commit.assert_awaited_once()
    assert chat.chat_config.discord_enabled is False
    assert chat.chat_config.discord_auth_status == "none"
    assert chat.chat_config.discord_auth_requester_id is None
    assert chat.chat_config.greeting == "keep me"


def test_server_list_embed_stays_within_discord_limit_and_shows_setu_off(monkeypatch):
    client = SimpleNamespace(
        get_guild=lambda guild_id: SimpleNamespace(name="Long server name " * 20)
    )
    monkeypatch.setattr(state, "discord_client", client)
    rows = [
        (10**17 + number, {"discord_r18_mode": "broken", "setu_enabled": False})
        for number in range(30)
    ]

    embed = server_list._build_discord_server_list_embed(rows)

    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    assert "OFF" in embed.fields[0].value
    assert f"of {len(rows)} servers" in embed.footer.text


def test_unknown_bot_channel_permissions_fail_closed(monkeypatch):
    monkeypatch.setattr(state, "discord_client", None)
    guild = SimpleNamespace(me=None)
    channel = SimpleNamespace(permissions_for=Mock())

    assert not permissions._can_view_channel(channel, guild)
    assert not permissions._can_read_message_history(channel, guild)
    channel.permissions_for.assert_not_called()

    assert permissions._can_view_channel(channel, None)
