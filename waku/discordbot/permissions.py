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
from .models import DiscordGuildSettings
from .settings import _discord_guild_settings

def _discord_allowed_mentions(allow_everyone: bool = False) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        users=True,
        roles=False,
        everyone=allow_everyone,
    )

def _is_discord_user_bot_admin(user: discord.abc.User) -> bool:
    return user.id in set(app_config.owners) | set(app_config.discord_admin_users)

def _is_discord_bot_admin(message: discord.Message) -> bool:
    return _is_discord_user_bot_admin(message.author) or _is_discord_server_admin(message)

def _is_discord_server_admin(message: discord.Message) -> bool:
    guild = message.guild
    if guild is not None and guild.owner_id == message.author.id:
        return True
    permissions = getattr(message.author, "guild_permissions", None)
    return bool(permissions and permissions.administrator)

def _can_manage_discord_config(message: discord.Message, settings: DiscordGuildSettings) -> bool:
    if _is_discord_bot_admin(message):
        return settings.enabled
    return settings.enabled and _is_discord_server_admin(message)

async def _send_admin_notice(message: discord.Message, text: str) -> None:
    embed = discord.Embed(
        description=text,
        color=discord.Color.blurple(),
        timestamp=datetime.now(),
    )
    await message.channel.send(embed=embed, reference=message, delete_after=10)
    try:
        await message.delete()
    except Exception:
        pass

def _channel_candidate_ids(message: discord.Message) -> set[int]:
    ids = {message.channel.id}
    if message.guild is not None:
        ids.add(message.guild.id)
    parent_id = getattr(message.channel, "parent_id", None)
    if parent_id is not None:
        ids.add(parent_id)
    parent = getattr(message.channel, "parent", None)
    if parent is not None:
        ids.add(parent.id)
    return ids

async def _channel_allowed(message: discord.Message) -> bool:
    if message.guild is None:
        return True

    settings = await _discord_guild_settings(message.guild)
    return settings.enabled

def _bot_member(guild: discord.Guild | None) -> discord.Member | None:
    if guild is None:
        return None
    member = guild.me
    if member is not None:
        return member
    if state.discord_client is not None and state.discord_client.user is not None:
        return guild.get_member(state.discord_client.user.id)
    return None

def _channel_permissions(channel: object, guild: discord.Guild | None) -> discord.Permissions | None:
    member = _bot_member(guild)
    permissions_for = getattr(channel, "permissions_for", None)
    if member is None or not callable(permissions_for):
        return None
    return permissions_for(member)

def _can_view_channel(channel: object, guild: discord.Guild | None) -> bool:
    permissions = _channel_permissions(channel, guild)
    return permissions is None or permissions.view_channel

def _can_read_message_history(channel: object, guild: discord.Guild | None) -> bool:
    permissions = _channel_permissions(channel, guild)
    return permissions is None or (
        permissions.view_channel and permissions.read_message_history
    )
