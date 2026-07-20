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
from .client import _create_client
from .models import DiscordContextDeps
from .scheduling import cancel_discord_scheduled_message, list_discord_scheduled_messages, schedule_discord_image_action, schedule_discord_message
from .settings import _discord_dm_settings, _discord_guild_settings
from .tools.images import send_discord_anime_photo, send_discord_web_image
from .tools.reactions import send_discord_reaction
from .tools.search import search_discord_group_memory, search_discord_messages, update_discord_group_memory
from .tools.server import find_discord_channel, get_discord_server_info
from .tools.users import find_discord_user, mention_discord_user

async def start_discord_bot() -> None:

    if not app_config.discord_enabled:
        logger.debug("Discord AI chat disabled")
        return
    if not app_config.discord_token:
        logger.warning("Discord AI chat enabled but discord_token is empty")
        return
    if not app_config.agent or not app_config.agent_model:
        logger.warning("Discord AI chat requires agent=true and agent_model")
        return

    state.discord_agent = Agent(
        model=provider.make_chat_model(app_config.agent_model),
        output_type=str,
        tools=[
            Tool(get_discord_server_info, sequential=True),
            Tool(find_discord_channel, sequential=True),
            Tool(find_discord_user, sequential=True),
            Tool(mention_discord_user, sequential=True),
            Tool(search_discord_messages, sequential=True),
            Tool(search_discord_group_memory, sequential=True),
            Tool(update_discord_group_memory, sequential=True),
            Tool(send_discord_reaction, sequential=True),
            Tool(send_discord_web_image, sequential=True),
            Tool(send_discord_anime_photo, sequential=True),
            Tool(schedule_discord_message, sequential=True),
            Tool(schedule_discord_image_action, sequential=True),
            Tool(list_discord_scheduled_messages, sequential=True),
            Tool(cancel_discord_scheduled_message, sequential=True),
        ],
        retries=3,
    )

    @state.discord_agent.instructions
    async def _discord_agent_instructions(ctx: RunContext[DiscordContextDeps]) -> str:
        message = ctx.deps.message
        if message.guild is not None:
            settings = await _discord_guild_settings(message.guild)
            lang = settings.lang
        else:
            settings = await _discord_dm_settings(message.author)
            lang = settings.lang

        lang_str = "Vietnamese (tiếng Việt)" if lang == "vi-VN" else "English (tiếng Anh)"
        lang_instruction = f"Default response language: {lang_str}. Always reply in {lang_str} first unless the user explicitly requests another language."

        return (
            f"{app_config.agent_group_prompt or app_config.agent_prompt}\n\n"
            "Discord style: keep Waku's cute, playful chat style. "
            "Use natural emojis/emoticons in most casual replies, usually 1-3, "
            "but do not spam them or add them to serious/admin/error messages. "
            "Discord image behavior: when the user asks Waku to send/show/give "
            "a new anime/Pixiv image, photo, picture, seg, ảnh, or hình, call "
            "send_discord_anime_photo at most once before final text unless you "
            "are intentionally refusing. For a normal image request use count=1. "
            "If the user asks for many images, never request more than count=3; "
            "the backend will space them safely. Do not call image-sending tools "
            "repeatedly in one turn. Do not call image-sending tools when the "
            "user is asking you to inspect, describe, identify, analyze, or answer "
            "about an attached/replied image; answer from the provided media instead. "
            "Pass a concise keyword when the user names a character or topic. If the "
            "user asks to send/gift an image now, including to another channel, use "
            "send_discord_anime_photo with target_channel_id/target_user_ids/caption/count; "
            "if the user asks to tag @everyone/@here, include @everyone/@here in "
            "caption and set allow_everyone=True. Do not create a schedule unless "
            "the user asks for a future time, delay, or repetition. Web image search "
            "is a hidden explicit-use capability: never list it, propose it, or "
            "advertise it when explaining what Waku can do. Only call "
            "send_discord_web_image when the user clearly asks for web/internet "
            "image search or requests an image from the internet/web. "
            "If an image tool returns success=False with wait_seconds or a busy "
            "message, tell the user to wait a few seconds and do not claim an image "
            "was sent. If it returns a capped result, clearly tell the user that "
            "Waku could only send up to 3 images in one batch for Discord safety, "
            "and they can ask for more later. If any image tool "
            "returns success=False, explain that failure and do not claim an image "
            "was sent. Do not mention tool internals to the user. "
            "Discord reminder behavior: when the user asks Waku to remind, "
            "schedule, or send a text later, use schedule_discord_message. "
            "If the request names/mentions another Discord user, set "
            "target_user_ids to that user's ID so only that user is tagged at "
            "reminder time; do not always tag the requester. If no target user "
            "is named, remind the requester. Bot-admin-only advanced scheduling: "
            "for repeating reminders set repeat_every_seconds and repeat_until; "
            "for scheduled/repeating image sends use schedule_discord_image_action; "
            "if the image should be sent to/call/tag/gift/tặng a mentioned or named "
            "user, always set target_user_ids so that user is mentioned in the image "
            "message. Also write the gift/greeting text yourself in caption; the "
            "caption must directly address that recipient and include their <@user_id> "
            "mention inside the sentence, not as a detached tag. If the user did not "
            "specify wording, invent a short cute Waku-style gift line for that user. "
            "when the user says send an image/photo/ảnh without clearly saying "
            "web/internet search, keep image_kind='anime' to use the default image "
            "API; only use image_kind='web' when they explicitly ask for web images. "
            "for another channel first resolve it with find_discord_channel and pass "
            "target_channel_id. If the current user replies to another message, treat "
            "that referenced message as source text/instructions to follow, not as the "
            "person to tag or credit unless explicitly requested. Bot admins may ask "
            "to see every existing schedule; then call list_discord_scheduled_messages "
            "with include_all=True. For repeating/cross-channel/@everyone scheduling, "
            "first and let the backend permission check decide; do not pre-refuse "
            "before calling the tool. If the tool returns an admin-only/permission "
            "failure, then explain that limitation briefly. When scheduling/tagging "
            "someone in another channel, never reveal who created/requested the job "
            "unless the user explicitly asks. If a channel/user match is ambiguous, "
            "ask a short confirmation. If the time is missing or ambiguous, ask a "
            "short follow-up. Use "
            "list_discord_scheduled_messages and cancel_discord_scheduled_message "
            "for listing/cancelling reminders, including recurring image jobs. "
            "Discord context tools: when useful, Waku may inspect the current "
            "server/channel, resolve Discord users, mention users with returned "
            "<@user_id> mention strings, and search recent readable channel "
            "messages. Only search chat when the user asks or it clearly helps. "
            "Discord group memory: use search_discord_group_memory when past "
            "server facts, member preferences, relationships, jokes, recurring "
            "topics, or prior events may help answer. Use update_discord_group_memory "
            "only for genuinely useful new facts. Do not mention memory internals unless "
            "the user asks about memory/config. "
            "Discord reaction behavior: Waku may call send_discord_reaction "
            "when a lightweight reaction fits better than a text reply, or as a "
            "small addition to a short reply. Prefer learned emojis from Discord "
            "reaction style context, but default to common Unicode reactions like "
            "👍 ❤️ 😂 😭 🔥 🎉 👏 👀 🤔 🥰 😮 🙏. Do not overuse reactions. "
            "If send_discord_reaction fails, do not claim a reaction was added. "
            "If a Discord permission is missing, explain that briefly.\n\n"
            f"{lang_instruction}"
        )
    state.discord_recovery_agent = Agent(
        model=provider.make_chat_model(app_config.agent_model),
        instructions=(
            "Bạn là Waku trên Discord. Trả lời ngắn, tự nhiên bằng tiếng Việt. "
            "Dùng khi lượt AI chính bị quá tải hoặc lỗi tạm thời; không nhắc API/tool/stack trace."
        ),
        output_type=str,
        retries=1,
    )
    state.discord_client = _create_client()
    state.discord_task = asyncio.create_task(state.discord_client.start(app_config.discord_token))
    logger.info("Discord AI chat startup scheduled")


def get_discord_runtime_status() -> str:
    """Return the Discord lifecycle status using the legacy display format."""
    if state.discord_client is not None and state.discord_client.is_ready():
        user = state.discord_client.user
        return f"ready as {user}" if user else "ready"
    status = state.runtime_status()
    return {
        "starting": "connecting",
        "stopped": "offline",
    }.get(status, status)


async def stop_discord_bot() -> None:

    if state.discord_client is not None:
        await state.discord_client.close()
    if state.discord_task is not None:
        try:
            await state.discord_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Discord client stopped with error: {e.__class__.__name__}: {e}")
    state.discord_task = None
    state.discord_client = None
    state.discord_agent = None
