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
from ..history import _discord_group_memory_user_id, _get_powermemory
from ..messages import _resolve_discord_channel
from ..models import DiscordContextDeps, DiscordMessageSearchEntry, DiscordMessageSearchResult
from ..permissions import _can_read_message_history
from ..settings import _discord_guild_settings
from ..utilities import _author_name, _channel_name, _message_text

async def search_discord_messages(
    ctx: RunContext[DiscordContextDeps],
    query: str,
    limit: int = 100,
    max_results: int = 8,
    channel_id: int | None = None,
) -> DiscordMessageSearchResult:
    """Search recent readable Discord messages.

    Searches the current channel by default. Only searches channels the bot can
    read and where it has Read Message History permission.

    Args:
        query: Text to search for. Case-insensitive substring match.
        limit: Number of recent messages to scan, capped for safety.
        max_results: Maximum matching snippets to return.
        channel_id: Optional Discord channel ID. Defaults to current channel.
    """
    message = ctx.deps.message
    guild = message.guild
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return DiscordMessageSearchResult(
            success=False,
            message="Search query is empty.",
        )

    channel = await _resolve_discord_channel(message, channel_id)
    if channel is None:
        return DiscordMessageSearchResult(
            success=False,
            message="Could not find that Discord channel.",
        )
    if not _can_read_message_history(channel, guild):
        return DiscordMessageSearchResult(
            success=False,
            message="I do not have permission to read message history in that channel.",
        )
    history = getattr(channel, "history", None)
    if not callable(history):
        return DiscordMessageSearchResult(
            success=False,
            message="That Discord channel does not support message history search.",
        )

    scan_limit = max(1, min(limit, 300))
    result_limit = max(1, min(max_results, 20))
    matches: list[DiscordMessageSearchEntry] = []
    searched = 0
    try:
        async for previous_message in history(limit=scan_limit):
            searched += 1
            content = _message_text(previous_message)
            if not content or normalized_query not in content.casefold():
                continue
            matches.append(
                DiscordMessageSearchEntry(
                    message_id=previous_message.id,
                    channel_id=previous_message.channel.id,
                    channel_name=_channel_name(previous_message),
                    author_id=previous_message.author.id,
                    author_name=_author_name(previous_message),
                    author_mention=previous_message.author.mention,
                    created_at=previous_message.created_at.isoformat(),
                    content=content[:800],
                    jump_url=previous_message.jump_url,
                )
            )
            if len(matches) >= result_limit:
                break
    except discord.Forbidden:
        return DiscordMessageSearchResult(
            success=False,
            message="Discord denied access to that channel history.",
            searched=searched,
        )
    except Exception as e:
        logger.error(f"Discord message search error: {e.__class__.__name__}: {e}")
        return DiscordMessageSearchResult(
            success=False,
            message="Discord message search failed.",
            searched=searched,
        )

    return DiscordMessageSearchResult(
        success=True,
        message=None if matches else "No matching recent messages found.",
        searched=searched,
        matches=matches,
    )

async def search_discord_group_memory(
    ctx: RunContext[DiscordContextDeps], query: str
) -> list[str]:
    """Search this Discord server's long-term group memory.

    Use this when the current Discord conversation may depend on facts Waku has
    learned about this server, its members, relationships, preferences, recurring
    topics, or past events. This memory is server-specific and separate from
    Telegram group memory.

    Args:
        query: Natural-language phrase describing what to retrieve.
    """
    message = ctx.deps.message
    if message.guild is None:
        return []
    settings = await _discord_guild_settings(message.guild)
    if not settings.group_memory_enabled:
        return []
    powermemory = _get_powermemory()
    if powermemory is None:
        return []
    search_query = query.strip()
    if not search_query:
        return []
    results = await powermemory.search(
        search_query,
        user_id=_discord_group_memory_user_id(message.guild.id),
        limit=10,
    )
    return [res.get("memory", "") for res in results.get("results", [])]

async def update_discord_group_memory(
    ctx: RunContext[DiscordContextDeps], content: str
) -> str:
    """Store a useful fact in this Discord server's long-term group memory.

    Use only for genuinely useful, non-trivial facts about the Discord server or
    its members. Do not store casual filler or information already obvious from
    the current message.

    Args:
        content: Concise factual statement to remember.
    """
    message = ctx.deps.message
    if message.guild is None:
        return "Discord group memory is only available in servers."
    settings = await _discord_guild_settings(message.guild)
    if not settings.group_memory_enabled:
        return "Discord group memory is disabled for this server."
    powermemory = _get_powermemory()
    if powermemory is None:
        return "Discord group memory system is not available."
    fact = content.strip()
    if not fact:
        raise ModelRetry("Memory content must not be empty.")
    try:
        result = await powermemory.add(
            fact,
            infer=True,
            user_id=_discord_group_memory_user_id(message.guild.id),
            prompt=(
                "You are Waku's Discord server memory. Extract useful facts, "
                "member preferences, relationships, recurring topics, or notable "
                "events worth remembering for this Discord server."
            ),
        )
        logger.debug(
            "update_discord_group_memory: stored memory "
            f"guild={message.guild.id} result={result}"
        )
        return f"Discord memory stored: {fact!r}"
    except Exception as e:
        logger.error(
            "update_discord_group_memory failed: "
            f"guild={message.guild.id} error={e.__class__.__name__}: {e}"
        )
        raise ModelRetry(f"Failed to store Discord memory: {e.__class__.__name__}: {e}")
