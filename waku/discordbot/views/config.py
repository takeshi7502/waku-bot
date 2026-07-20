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
from ..models import DiscordGuildSettings
from ..settings import _discord_dm_settings, _discord_guild_settings, _r18_mode_label, _rotate_discord_history_epoch, _set_discord_dm_settings, _set_discord_guild_settings

class DiscordConfigView(discord.ui.View):
    def __init__(self, guild: discord.Guild, settings: DiscordGuildSettings):
        super().__init__(timeout=300)
        self.guild = guild
        self.message: discord.Message | None = None
        self.pending_settings = DiscordGuildSettings(
            enabled=settings.enabled,
            r18_mode=settings.r18_mode,
            ai_reply=settings.ai_reply,
            group_memory_enabled=settings.group_memory_enabled,
            setu_enabled=settings.setu_enabled,
            lang=settings.lang,
        )

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.delete()
        except Exception:
            pass

    async def _sync_buttons(self) -> None:
        r18_button = self.children[0]
        if isinstance(r18_button, discord.ui.Button):
            r18_button.label = f"R18: {_r18_mode_label(self.pending_settings.setu_enabled, self.pending_settings.r18_mode)}"
            r18_button.style = (
                discord.ButtonStyle.secondary
                if not self.pending_settings.setu_enabled
                else discord.ButtonStyle.danger
                if self.pending_settings.r18_mode == 1
                else discord.ButtonStyle.secondary
                if self.pending_settings.r18_mode == 0
                else discord.ButtonStyle.primary
            )
        ai_button = self.children[1]
        if isinstance(ai_button, discord.ui.Button):
            ai_button.label = f"AI Reply: {'ON' if self.pending_settings.ai_reply else 'OFF'}"
            ai_button.style = (
                discord.ButtonStyle.success
                if self.pending_settings.ai_reply
                else discord.ButtonStyle.secondary
            )
        memory_button = self.children[2]
        if isinstance(memory_button, discord.ui.Button):
            memory_button.label = (
                f"Group Memory: {'ON' if self.pending_settings.group_memory_enabled else 'OFF'}"
            )
            memory_button.style = (
                discord.ButtonStyle.success
                if self.pending_settings.group_memory_enabled
                else discord.ButtonStyle.secondary
            )
        lang_button = self.children[3]
        if isinstance(lang_button, discord.ui.Button):
            lang_label = "Tiếng Việt" if self.pending_settings.lang == "vi-VN" else "English"
            lang_button.label = f"Language: {lang_label}"
            lang_button.style = discord.ButtonStyle.primary

    @discord.ui.button(label="R18", style=discord.ButtonStyle.secondary)
    async def toggle_r18(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.pending_settings.setu_enabled:
            current_state = 0
        else:
            current_state = self.pending_settings.r18_mode + 1
        
        next_state = (current_state + 1) % 4
        
        if next_state == 0:
            self.pending_settings.setu_enabled = False
            self.pending_settings.r18_mode = 0
        elif next_state == 1:
            self.pending_settings.setu_enabled = True
            self.pending_settings.r18_mode = 0
        elif next_state == 2:
            self.pending_settings.setu_enabled = True
            self.pending_settings.r18_mode = 1
        elif next_state == 3:
            self.pending_settings.setu_enabled = True
            self.pending_settings.r18_mode = 2

        await interaction.response.defer()
        await self._sync_buttons()
        await interaction.edit_original_response(
            content=_discord_config_text(self.pending_settings),
            view=self,
        )

    @discord.ui.button(label="AI Reply", style=discord.ButtonStyle.success)
    async def toggle_ai_reply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.pending_settings.ai_reply = not self.pending_settings.ai_reply
        await interaction.response.defer()
        await self._sync_buttons()
        await interaction.edit_original_response(
            content=_discord_config_text(self.pending_settings),
            view=self,
        )

    @discord.ui.button(label="Group Memory", style=discord.ButtonStyle.success)
    async def toggle_group_memory(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.pending_settings.group_memory_enabled = (
            not self.pending_settings.group_memory_enabled
        )
        await interaction.response.defer()
        await self._sync_buttons()
        await interaction.edit_original_response(
            content=_discord_config_text(self.pending_settings),
            view=self,
        )

    @discord.ui.button(label="Language", style=discord.ButtonStyle.primary)
    async def toggle_lang(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.pending_settings.lang == "vi-VN":
            self.pending_settings.lang = "en"
        else:
            self.pending_settings.lang = "vi-VN"
        await interaction.response.defer()
        await self._sync_buttons()
        await interaction.edit_original_response(
            content=_discord_config_text(self.pending_settings),
            view=self,
        )

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success)
    async def save_config(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await _set_discord_guild_settings(self.guild, self.pending_settings)
        await _rotate_discord_history_epoch(self.guild)
        logger.info(
            "Discord config saved: "
            f"guild={self.guild.name!r}({self.guild.id}) "
            f"r18_mode={self.pending_settings.r18_mode}"
            f"({_r18_mode_label(self.pending_settings.setu_enabled, self.pending_settings.r18_mode)}) "
            f"ai_reply={self.pending_settings.ai_reply} "
            f"group_memory={self.pending_settings.group_memory_enabled} "
            f"setu_enabled={self.pending_settings.setu_enabled} "
            f"lang={self.pending_settings.lang}"
        )
        try:
            if interaction.message is not None:
                await interaction.message.delete()
        except Exception:
            pass
        self.stop()

class DiscordDMConfigView(discord.ui.View):
    def __init__(self, user: discord.abc.User, settings: DiscordGuildSettings):
        super().__init__(timeout=300)
        self.user = user
        self.message: discord.Message | None = None
        self.pending_settings = DiscordGuildSettings(
            enabled=True,
            r18_mode=0,
            ai_reply=settings.ai_reply,
            group_memory_enabled=False,
            setu_enabled=settings.setu_enabled,
            lang=settings.lang,
        )

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.delete()
        except Exception:
            pass

    async def _sync_buttons(self) -> None:
        ai_button = self.children[0]
        if isinstance(ai_button, discord.ui.Button):
            ai_button.label = f"AI Reply: {'ON' if self.pending_settings.ai_reply else 'OFF'}"
            ai_button.style = (
                discord.ButtonStyle.success
                if self.pending_settings.ai_reply
                else discord.ButtonStyle.secondary
            )
        lang_button = self.children[1]
        if isinstance(lang_button, discord.ui.Button):
            lang_label = "Tiếng Việt" if self.pending_settings.lang == "vi-VN" else "English"
            lang_button.label = f"Language: {lang_label}"
            lang_button.style = discord.ButtonStyle.primary

    @discord.ui.button(label="AI Reply", style=discord.ButtonStyle.success)
    async def toggle_ai_reply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.pending_settings.ai_reply = not self.pending_settings.ai_reply
        await interaction.response.defer()
        await self._sync_buttons()
        await interaction.edit_original_response(
            content=_discord_dm_config_text(self.pending_settings),
            view=self,
        )

    @discord.ui.button(label="Language", style=discord.ButtonStyle.primary)
    async def toggle_lang(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.pending_settings.lang == "vi-VN":
            self.pending_settings.lang = "en"
        else:
            self.pending_settings.lang = "vi-VN"
        await interaction.response.defer()
        await self._sync_buttons()
        await interaction.edit_original_response(
            content=_discord_dm_config_text(self.pending_settings),
            view=self,
        )

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success)
    async def save_config(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await _set_discord_dm_settings(self.user, self.pending_settings)
        logger.info(
            "Discord DM config saved: "
            f"user={self.user.id} ai_reply={self.pending_settings.ai_reply} lang={self.pending_settings.lang}"
        )
        try:
            if interaction.message is not None:
                await interaction.message.delete()
        except Exception:
            pass
        self.stop()

async def _new_discord_config_view(guild: discord.Guild) -> DiscordConfigView:
    settings = await _discord_guild_settings(guild)
    view = DiscordConfigView(guild, settings)
    await view._sync_buttons()
    return view

async def _new_discord_dm_config_view(user: discord.abc.User) -> DiscordDMConfigView:
    settings = await _discord_dm_settings(user)
    view = DiscordDMConfigView(user, settings)
    await view._sync_buttons()
    return view

def _discord_dm_config_text(settings: DiscordGuildSettings) -> str:
    lang_name = "Tiếng Việt (vi-VN)" if settings.lang == "vi-VN" else "English (en)"
    return (
        "**Waku DM config:**\n"
        f"AI Reply: `{'ON' if settings.ai_reply else 'OFF'}`\n"
        f"Language: `{lang_name}`\n"
        "\nWhen AI Reply is `OFF`, Waku will ignore normal DM chat messages. "
        "Commands like `!config` still work.\n"
        "\nPress `Save` to apply changes."
    )

def _discord_config_text(settings: DiscordGuildSettings) -> str:
    lang_name = "Tiếng Việt (vi-VN)" if settings.lang == "vi-VN" else "English (en)"
    return (
        "**Waku Bot Server config:**\n"
        "Server: `Authorized!`\n"
        f"AI Reply: `{'ON' if settings.ai_reply else 'OFF'}`\n"
        f"Group Memory: `{'ON' if settings.group_memory_enabled else 'OFF'}`\n"
        f"R18/SEG images: `{_r18_mode_label(settings.setu_enabled, settings.r18_mode)}`\n"
        f"Language: `{lang_name}`\n"
        "\nPress `Save` to apply changes."
    )
