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
from ..models import DiscordContextDeps, DiscordMentionResult, DiscordUserSearchResult
from ..utilities import _discord_user_info, _find_discord_users

async def find_discord_user(
    ctx: RunContext[DiscordContextDeps], query: str, limit: int = 5
) -> DiscordUserSearchResult:
    """Find Discord users in the current server/chat.

    Query may be a username, display name, server nickname, global name, mention,
    or numeric Discord user ID. Returns mention strings the AI can use to tag
    matching users.

    Args:
        query: User search text, mention, or user ID.
        limit: Maximum number of users to return.
    """
    users = await _find_discord_users(ctx.deps.message, query, limit)
    if not users:
        return DiscordUserSearchResult(
            success=False,
            message=f"No Discord users found for query: {query!r}.",
            users=[],
        )
    return DiscordUserSearchResult(
        success=True,
        users=[_discord_user_info(user, matched_by=query) for user in users],
    )

async def mention_discord_user(
    ctx: RunContext[DiscordContextDeps], query: str
) -> DiscordMentionResult:
    """Resolve a Discord user and return a mention string like <@123>.

    Use this when the user asks Waku to tag, ping, call, mention, or notify a
    specific Discord user. Put the returned `mention` directly in the final reply.

    Args:
        query: Username, display name, nickname, mention, or Discord user ID.
    """
    users = await _find_discord_users(ctx.deps.message, query, limit=1)
    if not users:
        return DiscordMentionResult(
            success=False,
            message=f"Could not resolve a Discord user for query: {query!r}.",
        )
    user_info = _discord_user_info(users[0], matched_by=query)
    return DiscordMentionResult(
        success=True,
        mention=user_info.mention,
        user=user_info,
    )
