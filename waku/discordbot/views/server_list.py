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
from ..permissions import _is_discord_user_bot_admin
from ..settings import _r18_mode_label

async def _discord_authorized_server_rows() -> list[tuple[int, dict]]:
    from sqlalchemy import select

    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        rows = await session.execute(select(ChatData.id, ChatData.config))
        return [
            (chat_id, config)
            for chat_id, config in rows.all()
            if config and config.get("discord_enabled", False)
        ]

def _build_discord_server_list_embed(rows: list[tuple[int, dict]]) -> discord.Embed:
    embed = discord.Embed(
        title="Authorized Discord Servers",
        description=(
            "Servers currently authorized for Waku Discord features.\n"
            "Use **Reload** to refresh this menu."
        ),
        color=0x8B5CF6,
        timestamp=datetime.now(UTC),
    )
    if not rows:
        embed.description = "No authorized Discord servers."
        embed.color = 0x64748B
    for guild_id, config in rows[:25]:
        guild = state.discord_client.get_guild(guild_id) if state.discord_client else None
        joined = guild is not None
        name = guild.name if joined else "Unknown / not currently joined"
        r18_mode = int(config.get("discord_r18_mode", 2 if config.get("discord_allow_r18", False) else 0))
        ai_reply = bool(config.get("discord_ai_reply", True))
        status = "🟢 Joined" if joined else "⚫ Not joined"
        embed.add_field(
            name=f"{'✅' if joined else '❔'} {name}"[:256],
            value=(
                f"**Guild ID:** `{guild_id}`\n"
                f"**Status:** {status}\n"
                f"**AI Reply:** `{'ON' if ai_reply else 'OFF'}`\n"
                f"**R18 images:** `{_r18_mode_label(r18_mode)}`"
            ),
            inline=False,
        )
    if len(rows) > 25:
        embed.set_footer(text=f"Showing first 25 of {len(rows)} servers • Last updated")
    else:
        embed.set_footer(text="Last updated")
    return embed

async def _remember_discord_server_menu(message: discord.Message) -> None:
    try:
        menus: list[dict] = await common.memttlcache.get(_DISCORD_SERVER_MENU_CACHE_KEY, [])
        menus = [
            item
            for item in menus
            if item.get("message_id") != message.id
            and item.get("channel_id") != message.channel.id
        ]
        menus.insert(
            0,
            {
                "channel_id": message.channel.id,
                "message_id": message.id,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        await common.memttlcache.set(
            _DISCORD_SERVER_MENU_CACHE_KEY,
            menus[:20],
            ttl=app_config.cachettl_agent_history,
        )
    except Exception as e:
        logger.debug(f"Failed to remember Discord server menu: {e.__class__.__name__}: {e}")

class DiscordServerListView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Reload",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id=_DISCORD_SERVER_LIST_RELOAD_ID,
    )
    async def reload_servers(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not _is_discord_user_bot_admin(interaction.user):
            await interaction.response.send_message(
                "Only bot admins can reload this menu.", ephemeral=True
            )
            return
        rows = await _discord_authorized_server_rows()
        await interaction.response.edit_message(
            embed=_build_discord_server_list_embed(rows),
            view=self,
        )
        if interaction.message is not None:
            await _remember_discord_server_menu(interaction.message)
        logger.info(
            "Discord server list menu reloaded: "
            f"user={interaction.user.id} servers={len(rows)}"
        )

async def _send_discord_server_list(message: discord.Message) -> None:
    rows = await _discord_authorized_server_rows()
    sent = await message.channel.send(
        embed=_build_discord_server_list_embed(rows),
        view=DiscordServerListView(),
    )
    await _remember_discord_server_menu(sent)
    logger.info(
        "Discord server list menu sent: "
        f"channel={message.channel.id} message={sent.id} servers={len(rows)}"
    )
