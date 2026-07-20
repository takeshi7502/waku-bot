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
from ..history import _remember_discord_reaction_style
from ..models import DiscordContextDeps, DiscordReactionResult
from ..utilities import _channel_name, _guild_name

async def send_discord_reaction(
    ctx: RunContext[DiscordContextDeps],
    emoji: str,
    target_message_id: int | None = None,
) -> DiscordReactionResult:
    """Add a Discord reaction emoji to the current user's message.

    Prefer common Unicode reactions such as 👍, ❤️, 😂, 😭, 🔥, 🎉, 👏, 👀,
    🤔, 😡, 🥰, 😮, or 🙏. You may use a learned custom Discord emoji only
    when it appears in Discord reaction style context.

    Args:
        emoji: The reaction emoji to add.
        target_message_id: Optional message ID in the same channel. Defaults to
            the current user's message.
    """
    message = ctx.deps.message
    emoji_text = (emoji or "").strip()
    if not emoji_text:
        return DiscordReactionResult(success=False, message="Reaction emoji is empty.")

    target = message
    if target_message_id is not None and target_message_id != message.id:
        try:
            target = await message.channel.fetch_message(target_message_id)
        except Exception as e:
            logger.warning(
                "Discord reaction target fetch failed: "
                f"channel={message.channel.id} target_message_id={target_message_id} "
                f"error={e.__class__.__name__}: {e}"
            )
            return DiscordReactionResult(
                success=False,
                message="Could not find the target message to react to.",
                emoji=emoji_text,
                target_message_id=target_message_id,
            )

    try:
        reaction_emoji: str | discord.PartialEmoji = emoji_text
        if _CUSTOM_EMOJI_RE.fullmatch(emoji_text):
            reaction_emoji = discord.PartialEmoji.from_str(emoji_text)
        await target.add_reaction(reaction_emoji)
        await _remember_discord_reaction_style(message)
        logger.info(
            "Discord reaction sent: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"user={message.author.id} target_message_id={target.id} emoji={emoji_text!r}"
        )
        return DiscordReactionResult(
            success=True,
            emoji=emoji_text,
            target_message_id=target.id,
        )
    except Exception as e:
        logger.warning(
            "Discord reaction failed: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"user={message.author.id} emoji={emoji_text!r} error={e.__class__.__name__}: {e}"
        )
        return DiscordReactionResult(
            success=False,
            message=f"Failed to add reaction: {e.__class__.__name__}",
            emoji=emoji_text,
            target_message_id=target.id,
        )
