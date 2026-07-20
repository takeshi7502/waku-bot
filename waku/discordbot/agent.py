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
from .history import _sanitize_discord_history
from .messages import _send_reply
from .models import DiscordContextDeps

def _discord_model_status_code(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    cause = getattr(error, "__cause__", None)
    cause_status = getattr(cause, "status_code", None)
    return cause_status if isinstance(cause_status, int) else None

def _is_discord_model_error(error: Exception) -> bool:
    return isinstance(
        error,
        (
            pydantic_ai.exceptions.ModelHTTPError,
            pydantic_ai.exceptions.ModelAPIError,
        ),
    )

def _is_discord_temporary_model_error(error: Exception) -> bool:
    status = _discord_model_status_code(error)
    if status in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    return isinstance(error, TimeoutError | asyncio.TimeoutError)

def _is_discord_history_error(error: Exception) -> bool:
    status = _discord_model_status_code(error)
    if status == 400:
        return True
    text = str(error).casefold()
    history_markers = (
        "tool_call",
        "tool call",
        "tool_calls",
        "tool response",
        "messages",
        "image_url",
        "content and tool_calls",
    )
    return isinstance(error, TypeError) or any(marker in text for marker in history_markers)

def _discord_fallback_reply(error: Exception) -> str:
    if _is_discord_temporary_model_error(error):
        return random.choice(_DISCORD_TEMPORARY_ERROR_REPLIES)
    if _is_discord_history_error(error):
        return "Waku vừa dọn lại ngữ cảnh Discord bị lệch, gọi lại mình lần nữa nha."
    return "Waku xử lý lượt Discord này chưa ổn, thử gọi lại mình sau chút nha."

async def _run_discord_agent_once(
    message: discord.Message,
    prompt: list[UserContent],
    history_key: str,
    message_history: list[ModelMessage],
    model_override,
) -> None:
    assert state.discord_agent is not None
    async with message.channel.typing():
        result = await state.discord_agent.run(
            user_prompt=prompt,
            message_history=message_history,
            deps=DiscordContextDeps(message=message),
            model=model_override,
        )
    await common.memttlcache.set(
        history_key,
        _sanitize_discord_history(result.all_messages()),
        ttl=app_config.cachettl_agent_history,
    )
    if result.output:
        await _send_reply(message, str(result.output))

async def _discord_recovery_reply(
    message: discord.Message,
    user_prompt: str,
    error: Exception,
) -> str:
    fallback = _discord_fallback_reply(error)
    if state.discord_recovery_agent is None or not _is_discord_model_error(error):
        return fallback
    try:
        status = _discord_model_status_code(error)
        recovery_prompt = (
            "Bạn là Waku trên Discord. Lượt xử lý chính vừa lỗi trước khi trả lời.\n"
            f"Loại lỗi: {error.__class__.__name__}; status={status or 'unknown'}.\n"
            "Nếu có vẻ là quá tải/tạm thời, hãy trả lời tự nhiên bằng tiếng Việt rằng "
            "Waku đang hơi quá tải hoặc nghẽn nhẹ và xin user gọi lại sau chút. "
            "Không nhắc stack trace, provider, API nội bộ, hay tool.\n"
            f"Tin nhắn user: {user_prompt[:1200]}"
        )
        result = await state.discord_recovery_agent.run(
            user_prompt=recovery_prompt,
            message_history=[],
        )
        text = str(result.output or "").strip()
        return text[:1800] if text else fallback
    except Exception as recovery_error:
        logger.debug(
            "Discord recovery agent failed: "
            f"{recovery_error.__class__.__name__}: {recovery_error}"
        )
        return fallback
