from __future__ import annotations

import asyncio
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
from .models import DiscordChannelInfo, DiscordUserInfo
from .permissions import _can_read_message_history

def _message_text(message: discord.Message) -> str:
    return (message.content or message.clean_content or "").strip()

def _clean_content(message: discord.Message) -> str:
    text = _message_text(message)
    text = _URL_RE.sub(lambda m: m.group(0), text)
    text = _MENTION_RE.sub("", text)
    return text.strip()

def _guild_name(message: discord.Message) -> str:
    return message.guild.name if message.guild else "Direct Message"

def _channel_name(message: discord.Message) -> str:
    channel = message.channel
    if isinstance(channel, discord.DMChannel):
        return "DM"
    return getattr(channel, "name", str(channel.id))

def _author_name(message: discord.Message) -> str:
    author = message.author
    return getattr(author, "display_name", author.name)

def _extract_discord_id(value: str) -> int | None:
    match = re.search(r"<@!?(\d+)>|<#(\d+)>|(\d{15,25})", value.strip())
    if not match:
        return None
    raw_id = next(group for group in match.groups() if group)
    return int(raw_id)

def _channel_info(channel: object) -> DiscordChannelInfo:
    category = getattr(getattr(channel, "category", None), "name", None)
    return DiscordChannelInfo(
        id=getattr(channel, "id", 0),
        name=getattr(channel, "name", str(getattr(channel, "id", "unknown"))),
        type=channel.__class__.__name__,
        mention=getattr(channel, "mention", None),
        category=category,
    )

def _user_text_fields(user: discord.User | discord.Member) -> list[str]:
    fields = [str(user.id), user.name, getattr(user, "display_name", "")]
    fields.append(getattr(user, "global_name", "") or "")
    fields.append(getattr(user, "nick", "") or "")
    return [field.casefold() for field in fields if field]

def _query_matches_user(user: discord.User | discord.Member, query: str) -> bool:
    normalized = query.strip().casefold().lstrip("@")
    if not normalized:
        return True
    query_id = _extract_discord_id(query)
    if query_id is not None:
        return user.id == query_id
    return any(normalized in field for field in _user_text_fields(user))

def _discord_user_info(
    user: discord.User | discord.Member, matched_by: str | None = None
) -> DiscordUserInfo:
    roles: list[str] | None = None
    if isinstance(user, discord.Member):
        roles = [role.name for role in user.roles if not role.is_default()]
        roles = roles[-8:] or None
    return DiscordUserInfo(
        id=user.id,
        name=user.name,
        display_name=getattr(user, "display_name", user.name),
        mention=user.mention,
        bot=user.bot,
        global_name=getattr(user, "global_name", None),
        nick=getattr(user, "nick", None),
        matched_by=matched_by,
        roles=roles,
    )

async def _find_discord_users(
    message: discord.Message, query: str, limit: int = 5
) -> list[discord.User | discord.Member]:
    limit = max(1, min(limit, 10))
    guild = message.guild
    query_id = _extract_discord_id(query)
    users: list[discord.User | discord.Member] = []
    seen: set[int] = set()

    def add_user(user: discord.User | discord.Member | None) -> None:
        if user is None or user.id in seen:
            return
        if query and not _query_matches_user(user, query):
            return
        seen.add(user.id)
        users.append(user)

    for mentioned_user in message.mentions:
        add_user(mentioned_user)
    add_user(message.author)

    if guild is not None and query_id is not None:
        add_user(guild.get_member(query_id))
        if query_id not in seen:
            try:
                add_user(await guild.fetch_member(query_id))
            except Exception as e:
                logger.debug(f"Discord fetch_member failed for {query_id}: {e}")
    if state.discord_client is not None and query_id is not None and query_id not in seen:
        add_user(state.discord_client.get_user(query_id))
        if query_id not in seen:
            try:
                add_user(await state.discord_client.fetch_user(query_id))
            except Exception as e:
                logger.debug(f"Discord fetch_user failed for {query_id}: {e}")

    if guild is not None:
        for member in guild.members:
            add_user(member)
            if len(users) >= limit:
                return users[:limit]
        if query.strip() and len(users) < limit:
            query_members = getattr(guild, "query_members", None)
            if callable(query_members):
                try:
                    members = await query_members(
                        query=query.strip().lstrip("@"),
                        limit=limit,
                        cache=True,
                    )
                    for member in members:
                        add_user(member)
                except Exception as e:
                    logger.debug(f"Discord query_members failed: {e}")

    if len(users) < limit and _can_read_message_history(message.channel, guild):
        history = getattr(message.channel, "history", None)
        if callable(history):
            try:
                async for previous_message in history(limit=100):
                    add_user(previous_message.author)
                    if len(users) >= limit:
                        break
            except Exception as e:
                logger.debug(f"Discord recent-author scan failed: {e}")

    return users[:limit]
