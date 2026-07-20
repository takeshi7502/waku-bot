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


@dataclass
class DiscordGuildSettings:
    enabled: bool = False
    r18_mode: int = 0
    ai_reply: bool = True
    group_memory_enabled: bool = True
    setu_enabled: bool = True
    lang: str = "vi-VN"

@dataclass
class DiscordContextDeps:
    message: discord.Message

@dataclass
class DiscordGroupMemoryMessage:
    guild_id: int
    channel_id: int
    message_id: int
    text: str
    sender_name: str
    sender_id: int
    created_at: datetime

@dataclass
class DiscordArtistInfo:
    name: str
    type: str | None = None
    username: str | None = None
    uid: str | None = None

@dataclass
class DiscordAnimePhotoInfo:
    title: str
    source_url: str
    r18: bool
    description: str
    artist: DiscordArtistInfo | None
    tags: list[str]

@dataclass
class DiscordAnimePhotoResult:
    success: bool = True
    message: str | None = None
    data: DiscordAnimePhotoInfo | None = None
    sent_count: int = 0
    requested_count: int = 1
    capped_count: int | None = None
    wait_seconds: int | None = None

@dataclass
class DiscordChannelInfo:
    id: int
    name: str
    type: str
    mention: str | None = None
    category: str | None = None

@dataclass
class DiscordServerInfo:
    id: int
    name: str
    member_count: int | None
    owner_id: int | None
    owner_mention: str | None
    current_channel: DiscordChannelInfo
    channels: list[DiscordChannelInfo]

@dataclass
class DiscordServerInfoResult:
    success: bool = True
    message: str | None = None
    data: DiscordServerInfo | None = None

@dataclass
class DiscordUserInfo:
    id: int
    name: str
    display_name: str
    mention: str
    bot: bool
    global_name: str | None = None
    nick: str | None = None
    matched_by: str | None = None
    roles: list[str] | None = None

@dataclass
class DiscordUserSearchResult:
    success: bool = True
    message: str | None = None
    users: list[DiscordUserInfo] | None = None

@dataclass
class DiscordMentionResult:
    success: bool = True
    message: str | None = None
    mention: str | None = None
    user: DiscordUserInfo | None = None

@dataclass
class DiscordMessageSearchEntry:
    message_id: int
    channel_id: int
    channel_name: str
    author_id: int
    author_name: str
    author_mention: str
    created_at: str
    content: str
    jump_url: str

@dataclass
class DiscordMessageSearchResult:
    success: bool = True
    message: str | None = None
    searched: int = 0
    matches: list[DiscordMessageSearchEntry] | None = None

@dataclass
class DiscordWebImageResult:
    success: bool = True
    message: str | None = None
    title: str | None = None
    source_url: str | None = None

@dataclass
class DiscordReactionResult:
    success: bool = True
    message: str | None = None
    emoji: str | None = None
    target_message_id: int | None = None

@dataclass
class _DiscordScheduledJob:
    job_id: str
    guild_id: int
    channel_id: int
    user_id: int
    run_time: datetime | None
    summary: str
    kind: str = "message"
    recurring: bool = False

@dataclass
class DiscordScheduleResult:
    success: bool = True
    message: str | None = None
