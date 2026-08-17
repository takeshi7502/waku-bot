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
from discord import app_commands
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
from .broadcast import send_discord_broadcast
from .constants import *  # noqa: F403
from .handlers import _handle_discord_admin_command, _handle_message, _maybe_handle_discord_media_request
from .history import _record_discord_group_memory, _remember_discord_emojis, _remember_discord_reaction_style
from .media import _send_discord_seg_interaction
from .messages import _is_seg_command, _matches_keyword, _should_wake
from .permissions import _channel_allowed
from .utilities import _channel_name, _guild_name, _message_text
from .views.authorization import DiscordAuthorizationRequestView, DiscordAuthorizationReviewView
from .views.server_list import DiscordServerListView

def _create_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True
    intents.members = DISCORD_MEMBERS_INTENT
    client = discord.Client(intents=intents)
    command_tree = app_commands.CommandTree(client)
    slash_commands_synced = False

    @command_tree.command(name="seg", description="Gửi ảnh anime/Pixiv")
    @app_commands.describe(keyword="Từ khoá tìm ảnh, có thể bỏ trống")
    async def seg(interaction: discord.Interaction, keyword: str | None = None) -> None:
        await _send_discord_seg_interaction(interaction, keyword)

    @command_tree.command(name="bc", description="Phát thông báo đến các server")
    @app_commands.describe(
        message="Nội dung thông báo",
        target="here, all, auth, unauth, hoặc ID server",
    )
    async def bc(
        interaction: discord.Interaction,
        message: str,
        target: str | None = None,
    ) -> None:
        await send_discord_broadcast(interaction, message, target)

    @client.event
    async def on_ready() -> None:
        nonlocal slash_commands_synced
        user = client.user
        if user is None:
            logger.warning("Discord client ready without user")
            return
        logger.success(f"Discord AI chat ready as {user} ({user.id})")
        if not state.server_list_view_registered:
            client.add_view(DiscordServerListView())
            client.add_view(DiscordAuthorizationRequestView())
            client.add_view(DiscordAuthorizationReviewView())
            state.server_list_view_registered = True
            logger.info("Discord persistent server/config views registered")
        if not slash_commands_synced:
            try:
                synced = await command_tree.sync()
            except discord.HTTPException as e:
                logger.warning(f"Discord slash command sync failed: {e}")
            else:
                slash_commands_synced = True
                logger.info(f"Discord slash commands synced: {len(synced)}")

    @client.event
    async def on_message(message: discord.Message) -> None:
        bot_user = client.user
        if bot_user is None or message.author.bot:
            return
        content = _message_text(message)
        if await _handle_discord_admin_command(message):
            return
        await _record_discord_group_memory(message)
        await _remember_discord_emojis(message)
        await _remember_discord_reaction_style(message)
        should_wake, prompt = await _should_wake(message, bot_user)
        if not should_wake:
            return
        logger.debug(
            "Discord wake: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"user={message.author.id} len={len(content)} "
            f"keyword={_matches_keyword(content)} "
            f"seg_command={_is_seg_command(content)} "
            f"allowed={await _channel_allowed(message)}"
        )
        if await _maybe_handle_discord_media_request(message):
            return
        await _handle_message(message, prompt)

    return client
