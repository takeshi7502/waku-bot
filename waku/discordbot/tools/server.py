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

from .. import state
from ..constants import *  # noqa: F403
from ..models import DiscordChannelInfo, DiscordContextDeps, DiscordServerInfo, DiscordServerInfoResult
from ..permissions import _can_view_channel
from ..utilities import _channel_info, _extract_discord_id

async def get_discord_server_info(
    ctx: RunContext[DiscordContextDeps], channel_limit: int = 25
) -> DiscordServerInfoResult:
    """Get information about the current Discord server and visible channels.

    Use this when the user asks about the Discord server, current channel,
    available channels, or general server context.

    Args:
        channel_limit: Maximum number of visible channels to return.
    """
    message = ctx.deps.message
    guild = message.guild
    if guild is None:
        return DiscordServerInfoResult(
            success=False,
            message="This is a DM, so there is no Discord server to inspect.",
        )

    limit = max(1, min(channel_limit, 50))
    channels: list[DiscordChannelInfo] = []
    guild_channels = sorted(
        guild.channels,
        key=lambda channel: (
            getattr(channel, "position", 0),
            getattr(channel, "name", ""),
        ),
    )
    for channel in guild_channels:
        if not _can_view_channel(channel, guild):
            continue
        channels.append(_channel_info(channel))
        if len(channels) >= limit:
            break

    owner_id = guild.owner_id
    return DiscordServerInfoResult(
        success=True,
        data=DiscordServerInfo(
            id=guild.id,
            name=guild.name,
            member_count=guild.member_count,
            owner_id=owner_id,
            owner_mention=f"<@{owner_id}>" if owner_id else None,
            current_channel=_channel_info(message.channel),
            channels=channels,
        ),
    )

async def find_discord_channel(
    ctx: RunContext[DiscordContextDeps], query: str, limit: int = 5
) -> DiscordServerInfoResult:
    """Find visible Discord channels in the current server.

    Use this before cross-channel scheduling when the user names a channel by
    mention, ID, or fuzzy name such as "thông báo" or "chat". If multiple
    channels match, ask the user to confirm which returned channel to use.

    Args:
        query: Channel mention, ID, or name text.
        limit: Maximum number of matching channels to return.
    """
    message = ctx.deps.message
    guild = message.guild
    if guild is None:
        return DiscordServerInfoResult(success=False, message="This is a DM, so there are no server channels.")
    normalized = query.strip().casefold().lstrip("#")
    query_id = _extract_discord_id(query)
    max_results = max(1, min(limit, 10))
    matches: list[DiscordChannelInfo] = []
    for channel in guild.channels:
        if not _can_view_channel(channel, guild):
            continue
        channel_id = getattr(channel, "id", 0)
        name = str(getattr(channel, "name", ""))
        if query_id is not None:
            is_match = channel_id == query_id
        elif not normalized:
            is_match = True
        else:
            name_folded = name.casefold()
            is_match = normalized == name_folded or normalized in name_folded
        if is_match:
            matches.append(_channel_info(channel))
            if len(matches) >= max_results:
                break
    if not matches:
        return DiscordServerInfoResult(success=False, message=f"No Discord channels found for query: {query!r}.")
    return DiscordServerInfoResult(
        success=True,
        data=DiscordServerInfo(
            id=guild.id,
            name=guild.name,
            member_count=guild.member_count,
            owner_id=guild.owner_id,
            owner_mention=f"<@{guild.owner_id}>" if guild.owner_id else None,
            current_channel=_channel_info(message.channel),
            channels=matches,
        ),
    )
