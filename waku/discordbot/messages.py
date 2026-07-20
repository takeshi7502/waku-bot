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
from .history import _discord_emoji_hint, _discord_reaction_hint
from .media import _discord_attachment_contents, _discord_sticker_contents, _find_artwork_url
from .permissions import _channel_allowed, _channel_candidate_ids
from .settings import _discord_dm_ai_reply_enabled, _discord_guild_settings
from .utilities import _author_name, _channel_name, _clean_content, _guild_name, _message_text

def _normalize_keyword(keyword: str) -> str:
    return keyword.strip().lower()

def _keyword_regex(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    return re.compile(
        rf"(^|[\s,，:：!！?？])({escaped})($|[\s,，:：!！?？])",
        re.IGNORECASE,
    )

def _discord_wake_keywords() -> list[str]:
    keywords = app_config.discord_keywords or app_config.bot_keywords
    return [keyword for keyword in keywords if keyword.strip()]

def _strip_bot_mention(content: str, bot_user_id: int) -> str:
    mention_forms = (f"<@{bot_user_id}>", f"<@!{bot_user_id}>")
    text = content
    for mention in mention_forms:
        text = text.replace(mention, "")
    return text.strip()

def _strip_keyword(content: str) -> str:
    text = content.strip()
    lowered = text.lower()
    for keyword in _discord_wake_keywords():
        normalized = _normalize_keyword(keyword)
        if not normalized:
            continue
        if lowered == normalized:
            return ""
        for sep in _KEYWORD_SEPARATORS:
            prefix = normalized + sep
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip()
        match = _keyword_regex(normalized).search(text)
        if match:
            start, end = match.span(2)
            return (text[:start] + text[end:]).strip(" \n\t,，:：!！?？")
    return text

def _matches_keyword(content: str) -> bool:
    text = content.strip()
    lowered = text.lower()
    if not text:
        return False
    for keyword in _discord_wake_keywords():
        normalized = _normalize_keyword(keyword)
        if not normalized:
            continue
        if lowered == normalized:
            return True
        if any(lowered.startswith(normalized + sep) for sep in _KEYWORD_SEPARATORS):
            return True
        if _keyword_regex(normalized).search(text):
            return True
    return False

def _is_seg_command(content: str) -> bool:
    lowered = content.strip().lower()
    if not lowered:
        return False
    return any(
        lowered == command or lowered.startswith(command + " ")
        for command in _SETU_COMMANDS
    )

def _is_reply_to_bot(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    ref = message.reference
    if ref is None:
        return False
    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot_user.id
    cached = getattr(ref, "cached_message", None)
    if isinstance(cached, discord.Message):
        return cached.author.id == bot_user.id
    return False

async def _should_wake(message: discord.Message, bot_user: discord.ClientUser) -> tuple[bool, str]:
    if message.author.bot:
        return False, ""
    if not isinstance(message.channel, discord.abc.Messageable):
        return False, ""

    content = _message_text(message)
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = bot_user in message.mentions
    replied_to_bot = _is_reply_to_bot(message, bot_user)
    keyword = _matches_keyword(content)
    seg_command = _is_seg_command(content)
    artwork_url = _find_artwork_url(content)

    if not content and not is_dm and not mentioned and not replied_to_bot:
        if not state.warned_empty_content:
            logger.warning(
                "Discord message content is empty; keyword/seg wake requires "
                "Message Content Intent to be enabled in Discord Developer Portal"
            )
            state.warned_empty_content = True

    if (
        not is_dm
        and not mentioned
        and not replied_to_bot
        and not keyword
        and not seg_command
        and not artwork_url
    ):
        return False, ""
    if is_dm and not await _discord_dm_ai_reply_enabled(message.author):
        logger.debug(f"Discord DM AI reply ignored because it is disabled: user={message.author.id}")
        return False, ""
    if not is_dm and not await _channel_allowed(message):
        logger.debug(
            "Discord wake ignored because Waku is disabled for server: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"candidate_ids={sorted(_channel_candidate_ids(message))}"
        )
        return False, ""
    if not is_dm and not seg_command and not artwork_url:
        settings = await _discord_guild_settings(message.guild)
        if not settings.ai_reply:
            logger.debug(
                "Discord AI reply ignored because it is disabled for server: "
                f"guild={_guild_name(message)!r} channel={_channel_name(message)!r}"
            )
            return False, ""

    prompt = _clean_content(message)
    if mentioned:
        prompt = _strip_bot_mention(prompt, bot_user.id)
    if keyword:
        prompt = _strip_keyword(prompt)
    if not prompt:
        prompt = "Please continue the conversation."
    return True, prompt

async def _resolve_discord_channel(
    message: discord.Message, channel_id: int | None
) -> object | None:
    if channel_id is None:
        return message.channel
    guild = message.guild
    channel = None
    if guild is not None:
        get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
        if callable(get_channel_or_thread):
            channel = get_channel_or_thread(channel_id)
        if channel is None:
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if channel is None and state.discord_client is not None:
        channel = state.discord_client.get_channel(channel_id)
    if channel is None and state.discord_client is not None:
        try:
            channel = await state.discord_client.fetch_channel(channel_id)
        except Exception as e:
            logger.debug(f"Discord fetch_channel failed for {channel_id}: {e}")
    return channel

async def _reply_context(message: discord.Message) -> str | None:
    ref = message.reference
    if ref is None:
        return None
    replied = ref.resolved
    if isinstance(replied, discord.Message):
        text = _clean_content(replied)
        if text:
            return (
                f"Referenced message to use as source/instructions, not as the requester/target: "
                f"author={_author_name(replied)} text={text[:1000]}"
            )
    return None

async def _build_prompt(message: discord.Message, user_prompt: str) -> tuple[list[UserContent], bool]:
    parts = [
        "ContextInfo[Discord chat]",
        f"Server: {_guild_name(message)}",
        f"Channel: {_channel_name(message)}",
        f"User: {_author_name(message)} (id={message.author.id})",
        f"Current time: {datetime.now().isoformat(timespec='seconds')}",
    ]
    reply_ctx = await _reply_context(message)
    replied = message.reference.resolved if message.reference else None
    if reply_ctx:
        parts.append(reply_ctx)
    emoji_hint = await _discord_emoji_hint(message)
    if emoji_hint:
        parts.append(emoji_hint)
    reaction_hint = await _discord_reaction_hint(message)
    if reaction_hint:
        parts.append(reaction_hint)
    mentioned_users = [user for user in message.mentions if not user.bot]
    if mentioned_users:
        parts.append("Mentioned Discord users in the current message:")
        for user in mentioned_users[:10]:
            nick = getattr(user, "nick", None)
            global_name = getattr(user, "global_name", None)
            parts.append(
                f"- id={user.id} mention=<@{user.id}> name={user.name} "
                f"display={user.display_name} global={global_name or ''} nick={nick or ''}"
            )
        parts.append(
            "If the user asks to tag/remind one of these users, use the id above for target_user_ids even if their @name is missing from the cleaned text."
        )
    attachment_summaries, attachment_contents = await _discord_attachment_contents(message)
    sticker_summaries, sticker_contents = await _discord_sticker_contents(message)
    replied_attachment_summaries: list[str] = []
    replied_attachment_contents: list[UserContent] = []
    replied_sticker_summaries: list[str] = []
    replied_sticker_contents: list[UserContent] = []
    if isinstance(replied, discord.Message):
        replied_attachment_summaries, replied_attachment_contents = await _discord_attachment_contents(
            replied, label="replied attachment"
        )
        replied_sticker_summaries, replied_sticker_contents = await _discord_sticker_contents(
            replied, label="replied sticker"
        )
    media_summaries = (
        attachment_summaries
        + sticker_summaries
        + replied_attachment_summaries
        + replied_sticker_summaries
    )
    if media_summaries:
        parts.append("User/replied media:")
        parts.extend(media_summaries)
        if replied_attachment_contents or replied_sticker_contents:
            parts.append(
                "The user is replying to a message that contains media. Treat that replied media as the image/sticker they want analyzed; do not send a new image unless explicitly asked."
            )
    parts.append("User message:")
    parts.append(user_prompt or "[No text message]")
    contents: list[UserContent] = ["\n".join(parts)]
    contents.extend(attachment_contents)
    contents.extend(sticker_contents)
    contents.extend(replied_attachment_contents)
    contents.extend(replied_sticker_contents)
    needs_multimodal = bool(
        attachment_contents
        or sticker_contents
        or replied_attachment_contents
        or replied_sticker_contents
    )
    return contents, needs_multimodal

def _split_reply(text: str) -> list[str]:
    chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
    if not chunks:
        return []

    max_messages = DISCORD_REPLY_MAX_MESSAGES
    if len(chunks) <= max_messages:
        return chunks

    grouped: list[str] = []
    total = len(chunks)
    base = total // max_messages
    remainder = total % max_messages
    index = 0
    for i in range(max_messages):
        size = base + (1 if i < remainder else 0)
        grouped.append("\n\n".join(chunks[index : index + size]))
        index += size
    return grouped

async def _send_reply(message: discord.Message, text: str) -> None:
    chunks = _split_reply(text)
    if not chunks:
        return
    delay_min = DISCORD_REPLY_DELAY_MIN
    delay_max = DISCORD_REPLY_DELAY_MAX
    for index, chunk in enumerate(chunks):
        if len(chunk) > 1900:
            chunk = chunk[:1900] + "…"
        await message.channel.send(
            chunk,
            reference=message if index == 0 else None,
            mention_author=False,
        )
        if index < len(chunks) - 1:
            await asyncio.sleep(random.uniform(delay_min, delay_max) + len(chunk) / 900)
