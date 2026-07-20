from __future__ import annotations

import asyncio
import copy
import io
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import md5

import discord
import httpx
import pydantic_ai
from ddgs import DDGS
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext, Tool, UserContent
from pydantic_ai.messages import (
    MULTI_MODAL_CONTENT_TYPES,
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)

from waku import common
from waku.config import app_config
from waku.i18n import i18n
from waku.logger import logger
from waku.plugins.agent import provider
from waku.plugins.agent.history import filter_empty_model_responses
from waku.services import manyacg as manyacg_service
from waku.services.manyacg import manyacg_client

from . import state
from .constants import *  # noqa: F403
from .models import DiscordGuildSettings

def _discord_settings_cache_key(guild_id: int) -> str:
    return f"{_DISCORD_GUILD_SETTINGS_CACHE_PREFIX}{guild_id}"

def _history_epoch_key(guild_id: int) -> str:
    return f"discord_history_epoch:{guild_id}"

async def _discord_history_epoch(guild: discord.Guild | None) -> str:
    if guild is None:
        return "dm"
    epoch = await common.memttlcache.get(_history_epoch_key(guild.id), "0")
    return str(epoch)

async def _rotate_discord_history_epoch(guild: discord.Guild) -> None:
    epoch = str(datetime.now(UTC).timestamp())
    await common.memttlcache.set(
        _history_epoch_key(guild.id),
        epoch,
        ttl=app_config.cachettl_agent_history,
    )
    logger.info(f"Discord AI history reset: guild={guild.name!r}({guild.id}) epoch={epoch}")

async def _history_key(message: discord.Message) -> str:
    epoch = await _discord_history_epoch(message.guild)
    return f"discord_message_history:{epoch}:{message.channel.id}:{message.author.id}"

def _waiting_key(user_id: int) -> str:
    return f"discord_agent_waiting:{user_id}"

def _discord_dm_config_id(user_id: int) -> int:
    return -abs(int(user_id))

async def _discord_guild_settings(guild: discord.Guild | None) -> DiscordGuildSettings:
    if guild is None:
        return DiscordGuildSettings(enabled=True, group_memory_enabled=False)
    cache_key = _discord_settings_cache_key(guild.id)
    cached = await common.memttlcache.get(cache_key)
    if isinstance(cached, DiscordGuildSettings):
        return cached
    try:
        from waku.database.db import AsyncSessionFactory
        from waku.database.models import ChatData

        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, guild.id)
            if chat is None:
                chat = ChatData(id=guild.id, title=guild.name, username=None)
                session.add(chat)
                await session.commit()
            config = chat.chat_config
        settings = DiscordGuildSettings(
            enabled=config.discord_enabled,
            r18_mode=max(0, min(2, int(config.discord_r18_mode))),
            ai_reply=config.discord_ai_reply,
            group_memory_enabled=config.group_memory_enabled,
            setu_enabled=config.setu_enabled,
            lang=config.lang,
        )
        await common.memttlcache.set(
            cache_key,
            settings,
            ttl=_DISCORD_GUILD_SETTINGS_CACHE_TTL,
        )
        return settings
    except Exception as e:
        logger.error(f"Failed to load Discord guild settings from DB: {e}")
        return DiscordGuildSettings(enabled=False)

async def _set_discord_guild_settings(
    guild: discord.Guild, settings: DiscordGuildSettings
) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild.id)
        if chat is None:
            chat = ChatData(id=guild.id, title=guild.name, username=None)
            session.add(chat)
            await session.flush()
        config = chat.chat_config
        config.discord_enabled = settings.enabled
        config.discord_muted = False
        config.discord_allow_r18 = settings.r18_mode != 0
        config.discord_r18_mode = max(0, min(2, int(settings.r18_mode)))
        config.discord_ai_reply = settings.ai_reply
        config.discord_auth_status = DISCORD_AUTH_STATUS_NONE
        config.discord_auth_requester_id = None
        config.discord_auth_channel_id = None
        config.discord_auth_requested_at = None
        config.discord_auth_rejection_reason = None
        config.discord_auth_review_messages = None
        config.group_memory_enabled = settings.group_memory_enabled
        config.setu_enabled = settings.setu_enabled
        config.lang = settings.lang
        chat.chat_config = config
        await session.commit()
    await common.memttlcache.delete(f"chat_config:{guild.id}")
    await common.memttlcache.delete(_discord_settings_cache_key(guild.id))

async def _delete_discord_guild_settings(guild: discord.Guild) -> None:
    await _delete_discord_guild_settings_by_id(guild.id)

async def _set_discord_guild_settings_by_id(
    guild_id: int, guild_name: str | None, settings: DiscordGuildSettings
) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild_id)
        if chat is None:
            chat = ChatData(id=guild_id, title=guild_name or str(guild_id), username=None)
            session.add(chat)
            await session.flush()
        elif guild_name:
            chat.title = guild_name
        config = chat.chat_config
        config.discord_enabled = settings.enabled
        config.discord_muted = False
        config.discord_allow_r18 = settings.r18_mode != 0
        config.discord_r18_mode = max(0, min(2, int(settings.r18_mode)))
        config.discord_ai_reply = settings.ai_reply
        config.discord_auth_status = DISCORD_AUTH_STATUS_NONE
        config.discord_auth_requester_id = None
        config.discord_auth_channel_id = None
        config.discord_auth_requested_at = None
        config.discord_auth_rejection_reason = None
        config.discord_auth_review_messages = None
        config.group_memory_enabled = settings.group_memory_enabled
        config.setu_enabled = settings.setu_enabled
        config.lang = settings.lang
        chat.chat_config = config
        await session.commit()
    await common.memttlcache.delete(f"chat_config:{guild_id}")
    await common.memttlcache.delete(_discord_settings_cache_key(guild_id))

async def _delete_discord_guild_settings_by_id(guild_id: int) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild_id)
        if chat is not None:
            await session.delete(chat)
            await session.commit()
    await common.memttlcache.delete(f"chat_config:{guild_id}")
    await common.memttlcache.delete(_discord_settings_cache_key(guild_id))

async def _discord_auth_config(guild_id: int, guild_name: str | None = None):
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatConfig, ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild_id)
        if chat is None:
            return ChatConfig()
        return chat.chat_config


async def _submit_discord_auth_request(
    guild_id: int,
    guild_name: str,
    requester_id: int,
    channel_id: int,
):
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with state._discord_auth_lock(guild_id):
        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, guild_id)
            if chat is None:
                chat = ChatData(id=guild_id, title=guild_name, username=None)
                session.add(chat)
                await session.flush()
            else:
                chat.title = guild_name
            config = chat.chat_config
            if config.discord_enabled or config.discord_auth_status == DISCORD_AUTH_STATUS_PENDING:
                return config, False
            config.discord_auth_status = DISCORD_AUTH_STATUS_PENDING
            config.discord_auth_requester_id = requester_id
            config.discord_auth_channel_id = channel_id
            config.discord_auth_requested_at = datetime.now(UTC).isoformat()
            config.discord_auth_rejection_reason = None
            config.discord_auth_review_messages = []
            chat.chat_config = config
            await session.commit()
        await common.memttlcache.delete(_discord_settings_cache_key(guild_id))
        return config, True


async def _set_discord_auth_review_messages(guild_id: int, messages: list[dict]) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild_id)
        if chat is None:
            return
        config = chat.chat_config
        if config.discord_auth_status != DISCORD_AUTH_STATUS_PENDING:
            return
        config.discord_auth_review_messages = messages
        chat.chat_config = config
        await session.commit()


async def _reset_discord_auth_request(guild_id: int) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with state._discord_auth_lock(guild_id):
        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, guild_id)
            if chat is None:
                return
            config = chat.chat_config
            if config.discord_enabled:
                return
            config.discord_auth_status = DISCORD_AUTH_STATUS_NONE
            config.discord_auth_requester_id = None
            config.discord_auth_channel_id = None
            config.discord_auth_requested_at = None
            config.discord_auth_rejection_reason = None
            config.discord_auth_review_messages = None
            chat.chat_config = config
            await session.commit()


async def _approve_discord_auth_request(guild_id: int):
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with state._discord_auth_lock(guild_id):
        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, guild_id)
            if chat is None:
                return None
            config = chat.chat_config
            if config.discord_auth_status != DISCORD_AUTH_STATUS_PENDING:
                return None
            snapshot = copy.deepcopy(config)
            config.discord_enabled = True
            config.discord_muted = False
            config.discord_r18_mode = 0
            config.discord_allow_r18 = False
            config.discord_ai_reply = True
            config.group_memory_enabled = True
            config.setu_enabled = True
            config.discord_auth_status = DISCORD_AUTH_STATUS_NONE
            config.discord_auth_requester_id = None
            config.discord_auth_channel_id = None
            config.discord_auth_requested_at = None
            config.discord_auth_rejection_reason = None
            config.discord_auth_review_messages = None
            chat.chat_config = config
            await session.commit()
        await common.memttlcache.delete(f"chat_config:{guild_id}")
        await common.memttlcache.delete(_discord_settings_cache_key(guild_id))
        return snapshot


async def _reject_discord_auth_request(guild_id: int, reason: str):
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with state._discord_auth_lock(guild_id):
        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, guild_id)
            if chat is None:
                return None
            config = chat.chat_config
            if config.discord_auth_status != DISCORD_AUTH_STATUS_PENDING:
                return None
            config.discord_enabled = False
            config.discord_auth_status = DISCORD_AUTH_STATUS_REJECTED
            config.discord_auth_rejection_reason = reason.strip()[:1000]
            chat.chat_config = config
            await session.commit()
        await common.memttlcache.delete(_discord_settings_cache_key(guild_id))
        return config


async def _discord_dm_settings(user: discord.abc.User) -> DiscordGuildSettings:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    dm_id = _discord_dm_config_id(user.id)
    try:
        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, dm_id)
            if chat is None:
                return DiscordGuildSettings(
                    enabled=True,
                    r18_mode=0,
                    ai_reply=True,
                    group_memory_enabled=False,
                    setu_enabled=True,
                    lang="vi-VN",
                )
            config = chat.chat_config
        return DiscordGuildSettings(
            enabled=True,
            r18_mode=0,
            ai_reply=config.discord_ai_reply,
            group_memory_enabled=False,
            setu_enabled=config.setu_enabled,
            lang=config.lang,
        )
    except Exception as e:
        logger.error(f"Failed to load Discord DM settings from DB: user={user.id} error={e}")
        return DiscordGuildSettings(enabled=True, r18_mode=0, ai_reply=True, group_memory_enabled=False)

async def _set_discord_dm_settings(
    user: discord.abc.User, settings: DiscordGuildSettings
) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    dm_id = _discord_dm_config_id(user.id)
    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, dm_id)
        if chat is None:
            chat = ChatData(id=dm_id, title=f"Discord DM {user}", username=str(user))
            session.add(chat)
            await session.flush()
        config = chat.chat_config
        config.discord_enabled = True
        config.discord_muted = False
        config.discord_allow_r18 = False
        config.discord_r18_mode = 0
        config.discord_ai_reply = settings.ai_reply
        config.setu_enabled = settings.setu_enabled
        config.lang = settings.lang
        chat.chat_config = config
        await session.commit()
    await common.memttlcache.delete(f"chat_config:{dm_id}")

async def _discord_dm_ai_reply_enabled(user: discord.abc.User) -> bool:
    return (await _discord_dm_settings(user)).ai_reply

async def _discord_r18_mode(guild: discord.Guild | None) -> int:
    settings = await _discord_guild_settings(guild)
    if not settings.setu_enabled:
        return 0
    return settings.r18_mode

def _r18_mode_label(arg1, arg2=None) -> str:
    if arg2 is None:
        # Pattern: _r18_mode_label(r18_mode)
        r18_mode = int(arg1)
        setu_enabled = True
    elif isinstance(arg1, bool):
        # Pattern: _r18_mode_label(setu_enabled, r18_mode)
        setu_enabled = arg1
        r18_mode = int(arg2)
    elif isinstance(arg2, bool):
        # Pattern: _r18_mode_label(r18_mode, setu_enabled)
        r18_mode = int(arg1)
        setu_enabled = arg2
    else:
        # Fallback
        setu_enabled = bool(arg1)
        r18_mode = int(arg2)

    if not setu_enabled:
        return "OFF"
    return {
        0: "Safe Only",
        1: "R18 Only",
        2: "Mixed",
    }.get(r18_mode, "Safe Only")

async def _discord_ai_reply_enabled(guild: discord.Guild | None) -> bool:
    return (await _discord_guild_settings(guild)).ai_reply

def _contains_r18_keyword(text: str) -> bool:
    lowered = text.casefold()
    return any(keyword in lowered for keyword in _R18_KEYWORDS)
