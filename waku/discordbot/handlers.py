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
from .agent import _discord_recovery_reply, _is_discord_history_error, _run_discord_agent_once
from .history import _reaction_counter_key, _sanitize_discord_history, _strip_multimodal_history_for_text_model
from .media import _find_artwork_url, _send_discord_artwork, _send_discord_setu
from .messages import _build_prompt, _is_seg_command
from .models import DiscordGuildSettings
from .permissions import _can_manage_discord_config, _is_discord_bot_admin, _send_admin_notice
from .settings import _delete_discord_guild_settings, _delete_discord_guild_settings_by_id, _discord_guild_settings, _history_key, _set_discord_guild_settings, _set_discord_guild_settings_by_id, _waiting_key
from .state import _discord_agent_busy_timeout, _discord_agent_gate, _discord_agent_limit
from .utilities import _channel_name, _clean_content, _guild_name, _message_text
from .views.config import _discord_config_text, _discord_dm_config_text, _new_discord_config_view, _new_discord_dm_config_view
from .views.server_list import _send_discord_server_list

async def _handle_discord_admin_command(message: discord.Message) -> bool:
    prefix = DISCORD_COMMAND_PREFIX
    content = _message_text(message)
    if not content.startswith(prefix):
        return False
    parts = content[len(prefix) :].strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""
    if command not in {"waku", "unwaku", "config", "server"}:
        return False

    if message.guild is None:
        if command == "config":
            view = await _new_discord_dm_config_view(message.author)
            config_message = await message.channel.send(
                _discord_dm_config_text(view.pending_settings),
                view=view,
            )
            view.message = config_message
        elif command == "server" and _is_discord_bot_admin(message):
            await _send_discord_server_list(message)
        elif command in {"waku", "unwaku"} and _is_discord_bot_admin(message):
            if not args or not args.isdigit():
                await message.channel.send(f"Usage: `{prefix}{command} <server_id>`")
                return True
            guild_id = int(args)
            guild = state.discord_client.get_guild(guild_id) if state.discord_client else None
            if command == "waku":
                await _set_discord_guild_settings_by_id(
                    guild_id,
                    guild.name if guild else None,
                    DiscordGuildSettings(
                        enabled=True,
                        r18_mode=0,
                        ai_reply=True,
                        group_memory_enabled=True,
                        setu_enabled=True,
                    ),
                )
                await message.channel.send(
                    f"Waku has been authorized for `{guild.name if guild else guild_id}`."
                )
            else:
                await _delete_discord_guild_settings_by_id(guild_id)
                await message.channel.send(f"Waku has been unauthorized for `{guild_id}`.")
            logger.info(
                f"Discord DM admin command: user={message.author.id} command={command} guild={guild_id}"
            )
        return True

    settings = await _discord_guild_settings(message.guild)
    match command:
        case "waku":
            if not _is_discord_bot_admin(message):
                return True
            settings.enabled = True
            settings.r18_mode = 0
            settings.ai_reply = True
            settings.group_memory_enabled = True
            settings.setu_enabled = True
            await _set_discord_guild_settings(message.guild, settings)
            await _send_admin_notice(message, f"Waku has been authorized for **{message.guild.name}**.")
        case "unwaku":
            if not _is_discord_bot_admin(message):
                return True
            await _delete_discord_guild_settings(message.guild)
            await _send_admin_notice(message, f"Waku has been unauthorized for **{message.guild.name}**.")
        case "config":
            if not _can_manage_discord_config(message, settings):
                return True
            view = await _new_discord_config_view(message.guild)
            config_message = await message.channel.send(
                _discord_config_text(view.pending_settings),
                view=view,
                reference=message,
                mention_author=False,
            )
            view.message = config_message
            try:
                await message.delete()
            except Exception:
                pass
        case "server":
            return True
    logger.info(
        f"Discord admin command: guild={message.guild.id} user={message.author.id} command={command}"
    )
    return True

async def _maybe_handle_discord_media_request(message: discord.Message) -> bool:
    content = _clean_content(message) or _message_text(message)
    artwork_url = _find_artwork_url(content)
    if artwork_url:
        logger.info(
            f"Discord artwork request: guild={_guild_name(message)!r} "
            f"channel={_channel_name(message)!r} user={message.author.id} url={artwork_url}"
        )
        return await _send_discord_artwork(message, artwork_url)
    if _is_seg_command(content):
        logger.info(
            f"Discord command seg request: guild={_guild_name(message)!r} "
            f"channel={_channel_name(message)!r} user={message.author.id}"
        )
        return await _send_discord_setu(message)
    return False

async def _handle_message(message: discord.Message, user_prompt: str) -> None:
    if state.discord_agent is None:
        return

    waiting_key = _waiting_key(message.author.id)
    if await common.memstore.get(waiting_key):
        await message.channel.send("Thinking...", reference=message)
        return

    history_key = await _history_key(message)
    history: list[ModelMessage] = await common.memttlcache.get(history_key, [])
    history = _sanitize_discord_history(history)
    periodic_reaction_nudge = ""
    if app_config.agent_periodic_reaction_interval > 0:
        reaction_ctr: int = await common.memstore.get(_reaction_counter_key(message), 0)
        await common.memstore.set(_reaction_counter_key(message), reaction_ctr + 1)
        if reaction_ctr > 0 and reaction_ctr % app_config.agent_periodic_reaction_interval == 0:
            periodic_reaction_nudge = (
                "\n\nDiscord reaction nudge: if appropriate, call send_discord_reaction "
                "exactly once this turn with an emoji that matches the user's message."
            )

    prompt, needs_multimodal = await _build_prompt(message, user_prompt + periodic_reaction_nudge)
    model_override = (
        provider.make_chat_model(app_config.agent_model_multimodal)
        if needs_multimodal and app_config.agent_model_multimodal
        else None
    )
    model_history = (
        history
        if model_override is not None
        else _strip_multimodal_history_for_text_model(history)
    )
    model_history = model_history[-DISCORD_MESSAGE_HISTORY_LIMIT :]

    gate = _discord_agent_gate()
    acquired_gate = False
    await common.memstore.set(waiting_key, True)
    try:
        try:
            busy_timeout = _discord_agent_busy_timeout()
            if busy_timeout > 0:
                await asyncio.wait_for(gate.acquire(), timeout=busy_timeout)
            else:
                await gate.acquire()
            acquired_gate = True
        except TimeoutError:
            logger.info(
                "Discord agent busy timeout: "
                f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
                f"user={message.author.id} limit={_discord_agent_limit()}"
            )
            await message.channel.send(random.choice(_DISCORD_BUSY_REPLIES), reference=message)
            return

        try:
            await _run_discord_agent_once(
                message,
                prompt,
                history_key,
                model_history,
                model_override,
            )
            return
        except Exception as first_error:
            logger.warning(
                "Discord agent first attempt failed: "
                f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
                f"user={message.author.id} error={first_error.__class__.__name__}: {first_error}"
            )
            if _is_discord_history_error(first_error):
                await common.memttlcache.delete(history_key)

            retry_prompt = list(prompt)
            retry_prompt.append(
                "\n\n[Discord retry instruction] The previous attempt failed before replying. "
                "Retry with clean context. If the issue appears temporary or overload-related, "
                "answer naturally in Vietnamese that Waku is a bit overloaded and the user should try again soon."
            )
            try:
                await _run_discord_agent_once(
                    message,
                    retry_prompt,
                    history_key,
                    [],
                    model_override,
                )
                logger.info(
                    "Discord agent retry succeeded: "
                    f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
                    f"user={message.author.id}"
                )
                return
            except Exception as retry_error:
                logger.error(
                    "Discord agent retry failed: "
                    f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
                    f"user={message.author.id} error={retry_error.__class__.__name__}: {retry_error}"
                )
                if _is_discord_history_error(retry_error):
                    await common.memttlcache.delete(history_key)
                reply = await _discord_recovery_reply(message, user_prompt, retry_error)
                await message.channel.send(reply, reference=message)
    finally:
        if acquired_gate:
            gate.release()
        await common.memstore.delete(waiting_key)
