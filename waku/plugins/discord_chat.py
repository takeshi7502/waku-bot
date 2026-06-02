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
from ddgs import DDGS
from pydantic_ai import Agent, BinaryContent, RunContext, Tool, UserContent
from pydantic_ai.messages import (
    MULTI_MODAL_CONTENT_TYPES,
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)

from waku import common
from waku.config import app_config
from waku.logger import logger
from waku.plugins.agent import provider
from waku.plugins.agent.history import filter_empty_model_responses
from waku.services import manyacg as manyacg_service
from waku.services.manyacg import manyacg_client

_discord_client: discord.Client | None = None
_discord_task: asyncio.Task | None = None
_discord_agent: Agent[DiscordContextDeps, str] | None = None
_warned_empty_content = False
_server_list_view_registered = False
_discord_image_send_lock: asyncio.Lock | None = None

_DISCORD_IMAGE_BATCH_MAX = 3
_DISCORD_IMAGE_SEND_DELAY_SECONDS = 3.0
_DISCORD_IMAGE_BUSY_WAIT_SECONDS = 5

_DISCORD_SERVER_LIST_RELOAD_ID = "waku:discord_server_list:reload"
_DISCORD_SERVER_MENU_CACHE_KEY = "discord_server_menu_messages"


_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<@!?\d+>")
_KEYWORD_SEPARATORS = (" ", "\n", "\t", ",", ":", "，", "：", "!", "！", "?", "？")
_SETU_COMMANDS = ("setu", "/setu", "!setu", ".setu", "涩图", "色图")
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]"
)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:\d{15,25}>")
_R18_KEYWORDS = (
    "r18",
    "nsfw",
    "hentai",
    "ero",
    "ecchi",
    "nude",
    "naked",
    "porn",
    "sex",
    "lewd",
    "18+",
    "🔞",
)


@dataclass
class DiscordGuildSettings:
    enabled: bool = False
    r18_mode: int = 0
    ai_reply: bool = True


@dataclass
class DiscordContextDeps:
    message: discord.Message


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


_DISCORD_SCHEDULE_PREFIXES = {
    "discord_schedule_msg": ("message", False),
    "discord_schedule_image": ("image", False),
    "discord_schedule_repeat_msg": ("message", True),
    "discord_schedule_repeat_image": ("image", True),
}
_DISCORD_MIN_REPEAT_SECONDS = 60


def _clean_discord_job_id(job_id: str) -> str:
    return job_id.removesuffix("_memory")


def _parse_discord_scheduled_job(job) -> _DiscordScheduledJob | None:
    job_id = _clean_discord_job_id(str(job.id))
    parts = job_id.split(":")
    if len(parts) < 6 or parts[0] not in _DISCORD_SCHEDULE_PREFIXES:
        return None
    try:
        guild_id = int(parts[1])
        channel_id = int(parts[2])
        user_id = int(parts[3])
    except ValueError:
        return None
    args = list(getattr(job, "args", []) or [])
    kind, recurring = _DISCORD_SCHEDULE_PREFIXES[parts[0]]
    if kind == "image":
        image_kind = str(args[1]) if len(args) >= 2 else "image"
        query = str(args[2]) if len(args) >= 3 and args[2] else ""
        summary = f"{image_kind} image {query}".strip()[:120]
    else:
        summary = str(args[2])[:120] if len(args) >= 3 else str(args[1])[:120] if len(args) >= 2 else "message"
    return _DiscordScheduledJob(
        job_id=job_id,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        run_time=getattr(job, "next_run_time", None),
        summary=summary,
        kind=kind,
        recurring=recurring,
    )


def _discord_scheduled_jobs(
    guild_id: int,
    channel_id: int | None = None,
    user_id: int | None = None,
) -> list[_DiscordScheduledJob]:
    jobs: list[_DiscordScheduledJob] = []
    for job in common.jobqueue.get_all_jobs():
        parsed = _parse_discord_scheduled_job(job)
        if parsed is None or parsed.guild_id != guild_id:
            continue
        if channel_id is not None and parsed.channel_id != channel_id:
            continue
        if user_id is not None and parsed.user_id != user_id:
            continue
        jobs.append(parsed)
    return sorted(jobs, key=lambda item: item.run_time or datetime.max.replace(tzinfo=UTC))


def _format_discord_scheduled_job(job: _DiscordScheduledJob, index: int) -> str:
    when = job.run_time.isoformat() if job.run_time else "unknown time"
    repeat = "repeat " if job.recurring else ""
    return f"{index}. [{when}] <#{job.channel_id}> - {repeat}{job.kind}: {job.summary} - id={job.job_id}"


def _discord_allowed_mentions(allow_everyone: bool = False) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        users=True,
        roles=False,
        everyone=allow_everyone,
    )


def _discord_image_lock() -> asyncio.Lock:
    global _discord_image_send_lock
    if _discord_image_send_lock is None:
        _discord_image_send_lock = asyncio.Lock()
    return _discord_image_send_lock


async def _scheduled_discord_text_job(
    channel_id: int,
    target_user_ids: int | list[int] | tuple[int, ...],
    text: str,
    allow_everyone: bool = False,
    job_id: str | None = None,
    repeat_until: str | None = None,
) -> None:
    if repeat_until:
        try:
            until = datetime.fromisoformat(repeat_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if datetime.now(UTC) > until.astimezone(UTC):
                if job_id:
                    common.jobqueue.remove_job(job_id)
                return
        except ValueError:
            pass
    if _discord_client is None:
        logger.error("Scheduled Discord message failed: Discord client unavailable")
        return
    channel = _discord_client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _discord_client.fetch_channel(channel_id)
        except Exception as e:
            logger.error(
                "Scheduled Discord message failed to fetch channel: "
                f"channel={channel_id} error={e.__class__.__name__}: {e}"
            )
            return
    if not isinstance(channel, discord.abc.Messageable):
        logger.error(f"Scheduled Discord target is not messageable: channel={channel_id}")
        return
    try:
        reminder_text = text.strip()
        if isinstance(target_user_ids, int):
            user_ids = [target_user_ids]
        else:
            user_ids = [int(user_id) for user_id in target_user_ids]
        mentions = [f"<@{user_id}>" for user_id in dict.fromkeys(user_ids) if user_id > 0]
        prefix = " ".join(mention for mention in mentions if mention not in reminder_text)
        if prefix:
            reminder_text = f"{prefix} {reminder_text}"
        await channel.send(
            reminder_text,
            allowed_mentions=_discord_allowed_mentions(allow_everyone),
        )
        logger.debug(f"Scheduled Discord message sent successfully: channel={channel_id}")
    except Exception as e:
        logger.error(f"Scheduled Discord message failed: {e.__class__.__name__}: {e}")


async def _scheduled_discord_image_job(
    channel_id: int,
    image_kind: str,
    query: str | None = None,
    caption: str | None = None,
    allow_everyone: bool = False,
    job_id: str | None = None,
    repeat_until: str | None = None,
    target_user_ids: int | list[int] | tuple[int, ...] | None = None,
) -> None:
    if repeat_until:
        try:
            until = datetime.fromisoformat(repeat_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if datetime.now(UTC) > until.astimezone(UTC):
                if job_id:
                    common.jobqueue.remove_job(job_id)
                return
        except ValueError:
            pass
    if _discord_client is None:
        logger.error("Scheduled Discord image failed: Discord client unavailable")
        return
    channel = _discord_client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _discord_client.fetch_channel(channel_id)
        except Exception as e:
            logger.error(
                "Scheduled Discord image failed to fetch channel: "
                f"channel={channel_id} error={e.__class__.__name__}: {e}"
            )
            return
    if not isinstance(channel, discord.abc.Messageable):
        logger.error(f"Scheduled Discord image target is not messageable: channel={channel_id}")
        return

    caption_text = (caption or "").strip()
    user_ids: list[int] = []
    if isinstance(target_user_ids, int):
        user_ids = [target_user_ids]
    elif target_user_ids:
        user_ids = [int(user_id) for user_id in target_user_ids]
    mentions = [f"<@{user_id}>" for user_id in dict.fromkeys(user_ids) if user_id > 0]
    mention_prefix = " ".join(mention for mention in mentions if mention not in caption_text)
    caption_has_mention = any(mention in caption_text for mention in mentions)
    send_caption_as_content = bool(mentions and caption_text)
    if send_caption_as_content:
        message_content = caption_text if caption_has_mention else f"{' '.join(mentions)} {caption_text}"
        embed_caption_text = ""
    else:
        message_content = mention_prefix or None
        embed_caption_text = caption_text
    image_lock = _discord_image_lock()
    if image_lock.locked():
        logger.info(f"discord_image_queue_busy scheduled channel={channel_id} kind={image_kind}")
        return
    try:
        async with image_lock:
            if image_kind == "anime":
                guild = getattr(channel, "guild", None)
                r18_mode = await _discord_r18_mode(guild)
                if manyacg_client is None:
                    await channel.send("ManyACG is not configured, so Waku cannot send anime photos yet.")
                    return
                fetched = await _fetch_discord_anime_artwork((query or "").strip(), r18_mode=r18_mode)
                if fetched is None:
                    await channel.send("Could not fetch a scheduled anime image.")
                    return
                artwork, picture = fetched
                embed = _discord_artwork_embed(
                    title=artwork.title,
                    source_url=artwork.source_url,
                    image_url=picture.regular,
                    r18=artwork.r18,
                )
                if embed_caption_text:
                    embed.description = embed_caption_text[:4096]
                await channel.send(
                    content=message_content,
                    embed=embed,
                    allowed_mentions=_discord_allowed_mentions(allow_everyone),
                )
                logger.info(f"discord_image_send_success scheduled channel={channel_id} kind=anime")
                return

            search_query = (query or "anime image").strip()
            results = await _search_web_images(search_query)
            for result in results:
                image_url = result.get("image") or result.get("thumbnail")
                if not image_url:
                    continue
                downloaded = await _download_image_bytes(str(image_url))
                if downloaded is None:
                    continue
                data, content_type = downloaded
                file = discord.File(io.BytesIO(data), filename=_image_filename(content_type))
                title = str(result.get("title") or search_query)
                source_url = str(result.get("url") or image_url)
                embed = discord.Embed(
                    title=title[:256],
                    url=source_url,
                    description=embed_caption_text or f"Scheduled image: `{search_query[:120]}`",
                    color=0x8AC5FF,
                )
                embed.set_image(url=f"attachment://{file.filename}")
                await channel.send(
                    content=message_content,
                    embed=embed,
                    file=file,
                    allowed_mentions=_discord_allowed_mentions(allow_everyone),
                )
                logger.info(f"discord_image_send_success scheduled channel={channel_id} kind=web")
                return
            await channel.send("Could not find a downloadable scheduled image.")
    except Exception as e:
        logger.error(f"Scheduled Discord image failed: {e.__class__.__name__}: {e}")


@dataclass
class DiscordScheduleResult:
    success: bool = True
    message: str | None = None


async def schedule_discord_message(
    ctx: RunContext[DiscordContextDeps],
    schedule_time: str | None,
    text: str,
    send_immediately: bool = False,
    target_user_ids: list[int] | None = None,
    repeat_every_seconds: int | None = None,
    repeat_until: str | None = None,
    target_channel_id: int | None = None,
    allow_everyone: bool = False,
) -> DiscordScheduleResult:
    """Schedule a text message/reminder in the current Discord channel.

    Use this when the user asks Waku to remind them, schedule a reminder, or
    send a text message later. If the user did not provide a clear time, ask a
    follow-up question instead of guessing.

    Args:
        schedule_time: ISO 8601 datetime string in the future.
        text: Reminder/message text to send.
        send_immediately: If True, send text immediately without scheduling.
        target_user_ids: Discord user IDs to mention when the reminder fires.
            Use IDs from explicit user mentions/lookup in the user's request. If
            omitted, the tool mentions explicit non-bot users in the current
            message, otherwise the requester.
        repeat_every_seconds: Bot-admin-only repeat interval in seconds.
        repeat_until: Bot-admin-only ISO 8601 end time for recurring schedules.
        target_channel_id: Bot-admin-only target channel ID for cross-channel sends.
        allow_everyone: Bot-admin-only permission to allow @everyone/@here mentions.
    """
    message = ctx.deps.message
    if message.guild is None:
        return DiscordScheduleResult(success=False, message="Scheduling is only available in servers.")
    if not text or not text.strip():
        return DiscordScheduleResult(success=False, message="Reminder text is required.")

    is_admin = _is_discord_bot_admin(message)
    target_channel = await _resolve_discord_channel(message, target_channel_id)
    if target_channel is None or not isinstance(target_channel, discord.abc.Messageable):
        return DiscordScheduleResult(success=False, message="Target channel is not messageable or was not found.")
    target_channel_actual_id = getattr(target_channel, "id", message.channel.id)
    cross_channel = target_channel_actual_id != message.channel.id
    if cross_channel and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can schedule messages in another channel.")
    if repeat_every_seconds is not None and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can create recurring Discord schedules.")
    if allow_everyone and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can schedule @everyone/@here mentions.")
    if ("@everyone" in text or "@here" in text) and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can schedule @everyone/@here mentions.")

    target_ids = list(target_user_ids or [])
    bot_user_id = _discord_client.user.id if _discord_client and _discord_client.user else None
    if not target_ids:
        target_ids = [
            user.id
            for user in message.mentions
            if not user.bot and user.id != bot_user_id
        ]
    if not target_ids:
        target_ids = [message.author.id]
    target_ids = list(dict.fromkeys(target_ids))

    if send_immediately:
        mention_prefix = " ".join(f"<@{user_id}>" for user_id in target_ids)
        immediate_text = text.strip()
        if mention_prefix and not any(f"<@{user_id}>" in immediate_text for user_id in target_ids):
            immediate_text = f"{mention_prefix} {immediate_text}"
        await target_channel.send(
            immediate_text,
            reference=message if not cross_channel else None,
            allowed_mentions=_discord_allowed_mentions(allow_everyone),
        )
        return DiscordScheduleResult(success=True, message="Message sent.")

    if not schedule_time:
        return DiscordScheduleResult(success=False, message="A future schedule_time is required.")
    try:
        schedule_datetime = datetime.fromisoformat(schedule_time)
    except ValueError as e:
        return DiscordScheduleResult(success=False, message=f"Invalid schedule_time: {e}")
    local_tz = datetime.now().astimezone().tzinfo or UTC
    if schedule_datetime.tzinfo is None:
        schedule_datetime = schedule_datetime.replace(tzinfo=local_tz)
    schedule_datetime = schedule_datetime.astimezone(UTC)
    if schedule_datetime < datetime.now(UTC):
        return DiscordScheduleResult(success=False, message="schedule_time must be in the future.")

    until_datetime: datetime | None = None
    if repeat_every_seconds is not None:
        if repeat_every_seconds < _DISCORD_MIN_REPEAT_SECONDS:
            return DiscordScheduleResult(success=False, message=f"repeat_every_seconds must be at least {_DISCORD_MIN_REPEAT_SECONDS}.")
        if not repeat_until:
            return DiscordScheduleResult(success=False, message="repeat_until is required for recurring schedules.")
        try:
            until_datetime = datetime.fromisoformat(repeat_until)
        except ValueError as e:
            return DiscordScheduleResult(success=False, message=f"Invalid repeat_until: {e}")
        if until_datetime.tzinfo is None:
            until_datetime = until_datetime.replace(tzinfo=local_tz)
        until_datetime = until_datetime.astimezone(UTC)
        if until_datetime <= schedule_datetime:
            return DiscordScheduleResult(success=False, message="repeat_until must be after schedule_time.")

    text_content = text.strip()
    prefix = "discord_schedule_repeat_msg" if repeat_every_seconds else "discord_schedule_msg"
    job_key = (
        f"{prefix}:{message.guild.id}:{target_channel_actual_id}:{message.author.id}"
        f":{schedule_datetime.timestamp()}:{md5(text_content.encode()).hexdigest()}"
    )
    args = [
        target_channel_actual_id,
        target_ids,
        text_content,
        allow_everyone,
        job_key,
        until_datetime.isoformat() if until_datetime else None,
    ]
    if repeat_every_seconds:
        common.jobqueue.add_interval_job(
            job_key,
            func=_scheduled_discord_text_job,
            seconds=repeat_every_seconds,
            start_date=schedule_datetime.isoformat(),
            end_date=until_datetime.isoformat() if until_datetime else None,
            args=args,
        )
    else:
        common.jobqueue.add_onetime_job(
            job_key,
            run_date=schedule_datetime,
            func=_scheduled_discord_text_job,
            args=args,
        )
    logger.info(
        "Discord reminder scheduled: "
        f"guild={message.guild.id} channel={message.channel.id} "
        f"user={message.author.id} time={schedule_datetime.isoformat()}"
    )
    return DiscordScheduleResult(
        success=True,
        message=(
            f"{'Recurring schedule' if repeat_every_seconds else 'Scheduled'} for "
            f"{schedule_datetime.isoformat()} in <#{target_channel_actual_id}>"
        ),
    )


async def schedule_discord_image_action(
    ctx: RunContext[DiscordContextDeps],
    schedule_time: str | None,
    image_kind: str = "anime",
    query: str | None = None,
    caption: str | None = None,
    repeat_every_seconds: int | None = None,
    repeat_until: str | None = None,
    target_channel_id: int | None = None,
    allow_everyone: bool = False,
    target_user_ids: list[int] | None = None,
) -> DiscordScheduleResult:
    """Schedule a Discord image send action.

    Use this when the user asks Waku to send images later or repeatedly. If the
    user just says "send an image/photo/ảnh" without explicitly saying web/internet
    search, keep `image_kind="anime"` so the default anime image API is used.
    Only set `image_kind="web"` when the user clearly asks for web/internet image
    search. Recurring image schedules and cross-channel targets are bot-admin
    only. `image_kind` must be "anime" or "web".

    Args:
        schedule_time: ISO 8601 first send time.
        image_kind: "anime" for the default anime/API image, or "web" only when
            the user explicitly asks for web/internet image search.
        query: Optional image search keyword.
        caption: Optional text/caption to include in the scheduled image embed.
        repeat_every_seconds: Bot-admin-only repeat interval in seconds.
        repeat_until: Bot-admin-only ISO 8601 end time for recurring schedules.
        target_channel_id: Bot-admin-only target channel ID for cross-channel sends.
        allow_everyone: Bot-admin-only permission to allow @everyone/@here mentions.
        target_user_ids: Discord user IDs to mention when the scheduled image is sent.
            Use this when the user asks Waku to call/tag/invite/gửi tặng a user
            for the image. If omitted, explicit non-bot mentions in the current
            message and user mentions included in the caption are used as targets.
            When the request is a gift/tặng image to a person, always provide this
            and write an agent-authored caption/greeting for that person.
    """
    message = ctx.deps.message
    if message.guild is None:
        return DiscordScheduleResult(success=False, message="Scheduling is only available in servers.")
    if image_kind not in {"anime", "web"}:
        return DiscordScheduleResult(success=False, message="image_kind must be 'anime' or 'web'.")
    if not schedule_time:
        return DiscordScheduleResult(success=False, message="A future schedule_time is required.")

    is_admin = _is_discord_bot_admin(message)
    target_channel = await _resolve_discord_channel(message, target_channel_id)
    if target_channel is None or not isinstance(target_channel, discord.abc.Messageable):
        return DiscordScheduleResult(success=False, message="Target channel is not messageable or was not found.")
    target_channel_actual_id = getattr(target_channel, "id", message.channel.id)
    if target_channel_actual_id != message.channel.id and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can schedule images in another channel.")
    if repeat_every_seconds is not None and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can create recurring Discord image schedules.")
    if allow_everyone and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can schedule @everyone/@here mentions.")
    caption_text = (caption or "").strip()
    if ("@everyone" in caption_text or "@here" in caption_text) and not is_admin:
        return DiscordScheduleResult(success=False, message="Only bot admins can schedule @everyone/@here mentions.")

    target_ids = list(target_user_ids or [])
    bot_user_id = _discord_client.user.id if _discord_client and _discord_client.user else None
    if caption_text:
        target_ids.extend(int(user_id) for user_id in re.findall(r"<@!?(\d+)>", caption_text))
    if not target_ids:
        target_ids = [
            user.id
            for user in message.mentions
            if not user.bot and user.id != bot_user_id
        ]
    target_ids = [user_id for user_id in dict.fromkeys(target_ids) if user_id != bot_user_id]

    try:
        schedule_datetime = datetime.fromisoformat(schedule_time)
    except ValueError as e:
        return DiscordScheduleResult(success=False, message=f"Invalid schedule_time: {e}")
    local_tz = datetime.now().astimezone().tzinfo or UTC
    if schedule_datetime.tzinfo is None:
        schedule_datetime = schedule_datetime.replace(tzinfo=local_tz)
    schedule_datetime = schedule_datetime.astimezone(UTC)
    if schedule_datetime < datetime.now(UTC):
        return DiscordScheduleResult(success=False, message="schedule_time must be in the future.")

    until_datetime: datetime | None = None
    if repeat_every_seconds is not None:
        if repeat_every_seconds < _DISCORD_MIN_REPEAT_SECONDS:
            return DiscordScheduleResult(success=False, message=f"repeat_every_seconds must be at least {_DISCORD_MIN_REPEAT_SECONDS}.")
        if not repeat_until:
            return DiscordScheduleResult(success=False, message="repeat_until is required for recurring schedules.")
        try:
            until_datetime = datetime.fromisoformat(repeat_until)
        except ValueError as e:
            return DiscordScheduleResult(success=False, message=f"Invalid repeat_until: {e}")
        if until_datetime.tzinfo is None:
            until_datetime = until_datetime.replace(tzinfo=local_tz)
        until_datetime = until_datetime.astimezone(UTC)
        if until_datetime <= schedule_datetime:
            return DiscordScheduleResult(success=False, message="repeat_until must be after schedule_time.")

    query_text = (query or "").strip()
    hash_source = f"{image_kind}:{query_text}:{caption_text}"
    prefix = "discord_schedule_repeat_image" if repeat_every_seconds else "discord_schedule_image"
    job_key = (
        f"{prefix}:{message.guild.id}:{target_channel_actual_id}:{message.author.id}"
        f":{schedule_datetime.timestamp()}:{md5(hash_source.encode()).hexdigest()}"
    )
    args = [
        target_channel_actual_id,
        image_kind,
        query_text,
        caption_text,
        allow_everyone,
        job_key,
        until_datetime.isoformat() if until_datetime else None,
        target_ids,
    ]
    if repeat_every_seconds:
        common.jobqueue.add_interval_job(
            job_key,
            func=_scheduled_discord_image_job,
            seconds=repeat_every_seconds,
            start_date=schedule_datetime.isoformat(),
            end_date=until_datetime.isoformat() if until_datetime else None,
            args=args,
        )
    else:
        common.jobqueue.add_onetime_job(
            job_key,
            run_date=schedule_datetime,
            func=_scheduled_discord_image_job,
            args=args,
        )
    logger.info(
        "Discord image schedule created: "
        f"guild={message.guild.id} channel={target_channel_actual_id} user={message.author.id} "
        f"kind={image_kind} repeat={repeat_every_seconds} time={schedule_datetime.isoformat()}"
    )
    return DiscordScheduleResult(
        success=True,
        message=(
            f"{'Recurring image schedule' if repeat_every_seconds else 'Image scheduled'} for "
            f"{schedule_datetime.isoformat()} in <#{target_channel_actual_id}>"
        ),
    )


async def list_discord_scheduled_messages(
    ctx: RunContext[DiscordContextDeps], only_mine: bool = False, include_all: bool = False
) -> str:
    """List pending Discord reminders/scheduled messages in this server.

    Args:
        only_mine: If True, list only schedules created by the requester.
        include_all: Bot-admin-only flag for listing every schedule in the server.
            Use this when an admin asks for all current schedules/reminders.
    """
    message = ctx.deps.message
    if message.guild is None:
        return "Scheduling is only available in servers."
    if include_all and not _is_discord_bot_admin(message):
        return "Chỉ bot admin mới xem được toàn bộ lịch hẹn của server."
    user_id = message.author.id if only_mine and not include_all else None
    jobs = _discord_scheduled_jobs(message.guild.id, user_id=user_id)
    if not jobs:
        return "Không có lịch hẹn nào trong server này."
    title = "Toàn bộ lịch hẹn Discord hiện tại" if include_all else "Lịch hẹn Discord hiện tại"
    lines = [
        _format_discord_scheduled_job(job, index)
        for index, job in enumerate(jobs, start=1)
    ]
    return f"{title}:\n" + "\n".join(lines[:50])


async def cancel_discord_scheduled_message(
    ctx: RunContext[DiscordContextDeps],
    job_id: str | None = None,
    match_text: str | None = None,
    cancel_all: bool = False,
    only_mine: bool = False,
) -> DiscordScheduleResult:
    """Cancel pending Discord reminders/scheduled messages in this server."""
    message = ctx.deps.message
    if message.guild is None:
        return DiscordScheduleResult(success=False, message="Scheduling is only available in servers.")
    user_id = message.author.id if only_mine else None
    jobs = _discord_scheduled_jobs(message.guild.id, user_id=user_id)
    if not jobs:
        return DiscordScheduleResult(success=False, message="Không có lịch hẹn nào để huỷ.")

    if job_id:
        normalized = _clean_discord_job_id(job_id.strip())
        targets = [job for job in jobs if job.job_id == normalized]
    elif match_text:
        needle = match_text.casefold().strip()
        targets = [
            job
            for job in jobs
            if needle in job.job_id.casefold() or needle in job.summary.casefold()
        ]
    elif cancel_all:
        targets = jobs
    else:
        return DiscordScheduleResult(
            success=False,
            message="Cần job_id, match_text hoặc cancel_all=True để huỷ lịch hẹn.",
        )

    if not targets:
        return DiscordScheduleResult(success=False, message="Không tìm thấy lịch hẹn phù hợp.")
    if len(targets) > 1 and not cancel_all and not match_text:
        return DiscordScheduleResult(
            success=False,
            message="Tìm thấy nhiều lịch hẹn; hãy list rồi chọn job_id cụ thể.",
        )

    cancelled: list[str] = []
    for job in targets:
        common.jobqueue.remove_job(job.job_id)
        cancelled.append(_format_discord_scheduled_job(job, len(cancelled) + 1))
    logger.info(
        "Discord reminders cancelled: "
        f"guild={message.guild.id} user={message.author.id} count={len(cancelled)}"
    )
    return DiscordScheduleResult(
        success=True,
        message="Đã huỷ lịch hẹn:\n" + "\n".join(cancelled[:10]),
    )


def _history_epoch_key(guild_id: int) -> str:
    return f"discord_history_epoch:{guild_id}"


async def _discord_history_epoch(guild: discord.Guild | None) -> str:
    if guild is None:
        return "dm"
    epoch = await common.memttlcache.get(_history_epoch_key(guild.id), "0")
    return str(epoch)


async def _rotate_discord_history_epoch(guild: discord.Guild) -> None:
    epoch = str(datetime.now(UTC).timestamp())
    await common.memttlcache.set(
        _history_epoch_key(guild.id),
        epoch,
        ttl=app_config.cachettl_agent_history,
    )
    logger.info(f"Discord AI history reset: guild={guild.name!r}({guild.id}) epoch={epoch}")


async def _history_key(message: discord.Message) -> str:
    epoch = await _discord_history_epoch(message.guild)
    return f"discord_message_history:{epoch}:{message.channel.id}:{message.author.id}"


def _waiting_key(user_id: int) -> str:
    return f"discord_agent_waiting:{user_id}"


def _discord_dm_config_id(user_id: int) -> int:
    return -abs(int(user_id))


async def _discord_guild_settings(guild: discord.Guild | None) -> DiscordGuildSettings:
    if guild is None:
        return DiscordGuildSettings(enabled=True)
    try:
        from waku.database.db import AsyncSessionFactory
        from waku.database.models import ChatData

        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, guild.id)
            if chat is None:
                chat = ChatData(id=guild.id, title=guild.name, username=None)
                session.add(chat)
                await session.commit()
            config = chat.chat_config
        return DiscordGuildSettings(
            enabled=config.discord_enabled,
            r18_mode=max(0, min(2, int(config.discord_r18_mode))),
            ai_reply=config.discord_ai_reply,
        )
    except Exception as e:
        logger.error(f"Failed to load Discord guild settings from DB: {e}")
        return DiscordGuildSettings(enabled=False)


async def _set_discord_guild_settings(
    guild: discord.Guild, settings: DiscordGuildSettings
) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild.id)
        if chat is None:
            chat = ChatData(id=guild.id, title=guild.name, username=None)
            session.add(chat)
            await session.flush()
        config = chat.chat_config
        config.discord_enabled = settings.enabled
        config.discord_muted = False
        config.discord_allow_r18 = settings.r18_mode != 0
        config.discord_r18_mode = max(0, min(2, int(settings.r18_mode)))
        config.discord_ai_reply = settings.ai_reply
        chat.chat_config = config
        await session.commit()
    await common.memttlcache.delete(f"chat_config:{guild.id}")


async def _delete_discord_guild_settings(guild: discord.Guild) -> None:
    await _delete_discord_guild_settings_by_id(guild.id)


async def _set_discord_guild_settings_by_id(
    guild_id: int, guild_name: str | None, settings: DiscordGuildSettings
) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild_id)
        if chat is None:
            chat = ChatData(id=guild_id, title=guild_name or str(guild_id), username=None)
            session.add(chat)
            await session.flush()
        elif guild_name:
            chat.title = guild_name
        config = chat.chat_config
        config.discord_enabled = settings.enabled
        config.discord_muted = False
        config.discord_allow_r18 = settings.r18_mode != 0
        config.discord_r18_mode = max(0, min(2, int(settings.r18_mode)))
        config.discord_ai_reply = settings.ai_reply
        chat.chat_config = config
        await session.commit()
    await common.memttlcache.delete(f"chat_config:{guild_id}")


async def _delete_discord_guild_settings_by_id(guild_id: int) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild_id)
        if chat is not None:
            await session.delete(chat)
            await session.commit()
    await common.memttlcache.delete(f"chat_config:{guild_id}")


async def _discord_dm_settings(user: discord.abc.User) -> DiscordGuildSettings:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    dm_id = _discord_dm_config_id(user.id)
    try:
        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, dm_id)
            if chat is None:
                return DiscordGuildSettings(enabled=True, r18_mode=0, ai_reply=True)
            config = chat.chat_config
        return DiscordGuildSettings(
            enabled=True,
            r18_mode=0,
            ai_reply=config.discord_ai_reply,
        )
    except Exception as e:
        logger.error(f"Failed to load Discord DM settings from DB: user={user.id} error={e}")
        return DiscordGuildSettings(enabled=True, r18_mode=0, ai_reply=True)


async def _set_discord_dm_settings(
    user: discord.abc.User, settings: DiscordGuildSettings
) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    dm_id = _discord_dm_config_id(user.id)
    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, dm_id)
        if chat is None:
            chat = ChatData(id=dm_id, title=f"Discord DM {user}", username=str(user))
            session.add(chat)
            await session.flush()
        config = chat.chat_config
        config.discord_enabled = True
        config.discord_muted = False
        config.discord_allow_r18 = False
        config.discord_r18_mode = 0
        config.discord_ai_reply = settings.ai_reply
        chat.chat_config = config
        await session.commit()
    await common.memttlcache.delete(f"chat_config:{dm_id}")


async def _discord_dm_ai_reply_enabled(user: discord.abc.User) -> bool:
    return (await _discord_dm_settings(user)).ai_reply


async def _discord_r18_mode(guild: discord.Guild | None) -> int:
    return (await _discord_guild_settings(guild)).r18_mode


def _r18_mode_label(mode: int) -> str:
    return {
        0: "Safe Only",
        1: "R18 Only",
        2: "Mixed",
    }.get(mode, "Safe Only")


async def _discord_ai_reply_enabled(guild: discord.Guild | None) -> bool:
    return (await _discord_guild_settings(guild)).ai_reply


def _contains_r18_keyword(text: str) -> bool:
    lowered = text.casefold()
    return any(keyword in lowered for keyword in _R18_KEYWORDS)


def _emoji_key(message: discord.Message) -> str:
    guild_part = message.guild.id if message.guild else "dm"
    return f"discord_emoji_style:{guild_part}:{message.author.id}"


def _reaction_key(message: discord.Message) -> str:
    guild_part = message.guild.id if message.guild else "dm"
    return f"discord_reaction_style:{guild_part}:{message.author.id}"


def _reaction_counter_key(message: discord.Message) -> str:
    channel_id = getattr(message.channel, "id", "dm")
    return f"discord_periodic_reaction_counter:{channel_id}:{message.author.id}"


def _discord_reaction_candidates(message: discord.Message) -> list[str]:
    content = _message_text(message)
    candidates = _CUSTOM_EMOJI_RE.findall(content) + _EMOJI_RE.findall(content)
    for reaction in getattr(message, "reactions", []):
        emoji = getattr(reaction, "emoji", None)
        if isinstance(emoji, str):
            candidates.append(emoji)
        elif isinstance(emoji, discord.PartialEmoji | discord.Emoji):
            candidates.append(str(emoji))
    return candidates


async def _remember_discord_emojis(message: discord.Message) -> None:
    content = _message_text(message)
    emojis = _CUSTOM_EMOJI_RE.findall(content) + _EMOJI_RE.findall(content)
    if not emojis:
        return
    key = _emoji_key(message)
    existing: list[str] = await common.memttlcache.get(key, [])
    merged = (existing + emojis)[-40:]
    await common.memttlcache.set(key, merged, ttl=7 * 24 * 60 * 60)


async def _remember_discord_reaction_style(message: discord.Message) -> None:
    reactions = _discord_reaction_candidates(message)
    if not reactions:
        return
    key = _reaction_key(message)
    existing: list[str] = await common.memttlcache.get(key, [])
    merged = (existing + reactions)[-60:]
    await common.memttlcache.set(key, merged, ttl=7 * 24 * 60 * 60)


async def _discord_emoji_hint(message: discord.Message) -> str | None:
    emojis: list[str] = await common.memttlcache.get(_emoji_key(message), [])
    if not emojis:
        return None
    common_emojis = [emoji for emoji, _ in Counter(emojis).most_common(8)]
    if not common_emojis:
        return None
    return "User/server emoji style: " + " ".join(common_emojis)


async def _discord_reaction_hint(message: discord.Message) -> str | None:
    reactions: list[str] = await common.memttlcache.get(_reaction_key(message), [])
    if not reactions:
        return None
    common_reactions = [emoji for emoji, _ in Counter(reactions).most_common(10)]
    if not common_reactions:
        return None
    return "Discord reaction style: " + " ".join(common_reactions)


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


def _normalize_keyword(keyword: str) -> str:
    return keyword.strip().lower()


def _message_text(message: discord.Message) -> str:
    return (message.content or message.clean_content or "").strip()


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


def _is_setu_command(content: str) -> bool:
    lowered = content.strip().lower()
    if not lowered:
        return False
    return any(
        lowered == command or lowered.startswith(command + " ")
        for command in _SETU_COMMANDS
    )


def _find_artwork_url(content: str) -> str | None:
    for regex in manyacg_service.ARTWORK_ALL_REGEX:
        match = regex.search(content)
        if match:
            artwork_url = match.group()
            if not artwork_url.startswith("http"):
                artwork_url = "https://" + artwork_url
            return artwork_url
    return None


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


def _clean_content(message: discord.Message) -> str:
    text = _message_text(message)
    text = _URL_RE.sub(lambda m: m.group(0), text)
    text = _MENTION_RE.sub("", text)
    return text.strip()


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
    setu_command = _is_setu_command(content)
    artwork_url = _find_artwork_url(content)

    if not content and not is_dm and not mentioned and not replied_to_bot:
        global _warned_empty_content
        if not _warned_empty_content:
            logger.warning(
                "Discord message content is empty; keyword/setu wake requires "
                "Message Content Intent to be enabled in Discord Developer Portal"
            )
            _warned_empty_content = True

    if (
        not is_dm
        and not mentioned
        and not replied_to_bot
        and not keyword
        and not setu_command
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
    if not is_dm and not setu_command and not artwork_url:
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


def _guild_name(message: discord.Message) -> str:
    return message.guild.name if message.guild else "Direct Message"


def _channel_name(message: discord.Message) -> str:
    channel = message.channel
    if isinstance(channel, discord.DMChannel):
        return "DM"
    return getattr(channel, "name", str(channel.id))


def _author_name(message: discord.Message) -> str:
    author = message.author
    return getattr(author, "display_name", author.name)


def _extract_discord_id(value: str) -> int | None:
    match = re.search(r"<@!?(\d+)>|<#(\d+)>|(\d{15,25})", value.strip())
    if not match:
        return None
    raw_id = next(group for group in match.groups() if group)
    return int(raw_id)


def _channel_info(channel: object) -> DiscordChannelInfo:
    category = getattr(getattr(channel, "category", None), "name", None)
    return DiscordChannelInfo(
        id=getattr(channel, "id", 0),
        name=getattr(channel, "name", str(getattr(channel, "id", "unknown"))),
        type=channel.__class__.__name__,
        mention=getattr(channel, "mention", None),
        category=category,
    )


def _bot_member(guild: discord.Guild | None) -> discord.Member | None:
    if guild is None:
        return None
    member = guild.me
    if member is not None:
        return member
    if _discord_client is not None and _discord_client.user is not None:
        return guild.get_member(_discord_client.user.id)
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


def _user_text_fields(user: discord.User | discord.Member) -> list[str]:
    fields = [str(user.id), user.name, getattr(user, "display_name", "")]
    fields.append(getattr(user, "global_name", "") or "")
    fields.append(getattr(user, "nick", "") or "")
    return [field.casefold() for field in fields if field]


def _query_matches_user(user: discord.User | discord.Member, query: str) -> bool:
    normalized = query.strip().casefold().lstrip("@")
    if not normalized:
        return True
    query_id = _extract_discord_id(query)
    if query_id is not None:
        return user.id == query_id
    return any(normalized in field for field in _user_text_fields(user))


def _discord_user_info(
    user: discord.User | discord.Member, matched_by: str | None = None
) -> DiscordUserInfo:
    roles: list[str] | None = None
    if isinstance(user, discord.Member):
        roles = [role.name for role in user.roles if not role.is_default()]
        roles = roles[-8:] or None
    return DiscordUserInfo(
        id=user.id,
        name=user.name,
        display_name=getattr(user, "display_name", user.name),
        mention=user.mention,
        bot=user.bot,
        global_name=getattr(user, "global_name", None),
        nick=getattr(user, "nick", None),
        matched_by=matched_by,
        roles=roles,
    )


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
    if channel is None and _discord_client is not None:
        channel = _discord_client.get_channel(channel_id)
    if channel is None and _discord_client is not None:
        try:
            channel = await _discord_client.fetch_channel(channel_id)
        except Exception as e:
            logger.debug(f"Discord fetch_channel failed for {channel_id}: {e}")
    return channel


async def _find_discord_users(
    message: discord.Message, query: str, limit: int = 5
) -> list[discord.User | discord.Member]:
    limit = max(1, min(limit, 10))
    guild = message.guild
    query_id = _extract_discord_id(query)
    users: list[discord.User | discord.Member] = []
    seen: set[int] = set()

    def add_user(user: discord.User | discord.Member | None) -> None:
        if user is None or user.id in seen:
            return
        if query and not _query_matches_user(user, query):
            return
        seen.add(user.id)
        users.append(user)

    for mentioned_user in message.mentions:
        add_user(mentioned_user)
    add_user(message.author)

    if guild is not None and query_id is not None:
        add_user(guild.get_member(query_id))
        if query_id not in seen:
            try:
                add_user(await guild.fetch_member(query_id))
            except Exception as e:
                logger.debug(f"Discord fetch_member failed for {query_id}: {e}")
    if _discord_client is not None and query_id is not None and query_id not in seen:
        add_user(_discord_client.get_user(query_id))
        if query_id not in seen:
            try:
                add_user(await _discord_client.fetch_user(query_id))
            except Exception as e:
                logger.debug(f"Discord fetch_user failed for {query_id}: {e}")

    if guild is not None:
        for member in guild.members:
            add_user(member)
            if len(users) >= limit:
                return users[:limit]
        if query.strip() and len(users) < limit:
            query_members = getattr(guild, "query_members", None)
            if callable(query_members):
                try:
                    members = await query_members(
                        query=query.strip().lstrip("@"),
                        limit=limit,
                        cache=True,
                    )
                    for member in members:
                        add_user(member)
                except Exception as e:
                    logger.debug(f"Discord query_members failed: {e}")

    if len(users) < limit and _can_read_message_history(message.channel, guild):
        history = getattr(message.channel, "history", None)
        if callable(history):
            try:
                async for previous_message in history(limit=100):
                    add_user(previous_message.author)
                    if len(users) >= limit:
                        break
            except Exception as e:
                logger.debug(f"Discord recent-author scan failed: {e}")

    return users[:limit]


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


_DISCORD_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_DISCORD_MEDIA_MAX_BYTES = 8 * 1024 * 1024


def _discord_media_enabled() -> bool:
    return app_config.agent_multimodal and "photo" in app_config.agent_multimodal_inputs


async def _download_discord_media(url: str) -> tuple[bytes, str] | None:
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "WakuDiscordBot/1.0"}) as response:
            if response.status_code >= 400:
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in _DISCORD_IMAGE_CONTENT_TYPES:
                return None
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > _DISCORD_MEDIA_MAX_BYTES:
                    return None
            return bytes(data), content_type


async def _discord_attachment_contents(
    message: discord.Message, label: str = "attachment"
) -> tuple[list[str], list[UserContent]]:
    summaries: list[str] = []
    contents: list[UserContent] = []
    if not message.attachments:
        return summaries, contents
    for attachment in message.attachments[:4]:
        content_type = (attachment.content_type or "").split(";", 1)[0].lower()
        size_kb = max(1, int((attachment.size or 0) / 1024))
        summaries.append(
            f"- {label}: {attachment.filename} type={content_type or 'unknown'} size={size_kb}KB"
        )
        if not _discord_media_enabled() or content_type not in _DISCORD_IMAGE_CONTENT_TYPES:
            continue
        if attachment.size and attachment.size > _DISCORD_MEDIA_MAX_BYTES:
            summaries.append(f"  skipped: file is larger than {_DISCORD_MEDIA_MAX_BYTES // 1024 // 1024}MB")
            continue
        try:
            data = await attachment.read(use_cached=True)
        except Exception as e:
            logger.debug(f"Discord attachment download failed: {e.__class__.__name__}: {e}")
            downloaded = await _download_discord_media(attachment.url)
            if downloaded is None:
                continue
            data, content_type = downloaded
        if len(data) <= _DISCORD_MEDIA_MAX_BYTES:
            contents.append(BinaryContent(data=data, media_type=content_type))
    return summaries, contents


async def _discord_sticker_contents(
    message: discord.Message, label: str = "sticker"
) -> tuple[list[str], list[UserContent]]:
    summaries: list[str] = []
    contents: list[UserContent] = []
    if not message.stickers:
        return summaries, contents
    for sticker in message.stickers[:3]:
        sticker_format = getattr(sticker, "format", None)
        summaries.append(f"- {label}: {sticker.name} format={sticker_format}")
        if not _discord_media_enabled():
            continue
        url = getattr(sticker, "url", None)
        if not url:
            continue
        try:
            downloaded = await _download_discord_media(str(url))
        except Exception as e:
            logger.debug(f"Discord sticker download failed: {e.__class__.__name__}: {e}")
            continue
        if downloaded is None:
            continue
        data, content_type = downloaded
        contents.append(BinaryContent(data=data, media_type=content_type))
    return summaries, contents


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

    max_messages = max(1, app_config.discord_reply_max_messages)
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
    delay_min = max(0.0, app_config.discord_reply_delay_min)
    delay_max = max(delay_min, app_config.discord_reply_delay_max)
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


def _sanitize_discord_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Remove previous tool messages before reusing Discord history.

    DeepSeek's OpenAI-compatible endpoint is strict about historical `tool`
    messages: each one must immediately follow its matching assistant
    `tool_calls` message. Discord does not need old tool internals for memory, so
    we keep user/text assistant history and drop old tool call/return messages.
    Current-turn tool calls still work normally because this only sanitizes
    cached history before the next request.
    """
    cleaned: list[ModelMessage] = []
    removed = 0
    for msg in filter_empty_model_responses(messages):
        part_kinds = {getattr(part, "part_kind", "") for part in msg.parts}
        if part_kinds & {"tool-call", "tool-return", "retry-prompt"}:
            removed += 1
            continue
        cleaned.append(msg)
    if removed:
        logger.debug(f"Discord history sanitized: removed {removed} tool messages")
    return cleaned


def _strip_multimodal_history_for_text_model(
    history: list[ModelMessage],
) -> list[ModelMessage]:
    """Return Discord history safe for text-only providers.

    After a Discord image/sticker turn, pydantic-ai stores multimodal parts in
    history. If the next turn goes back to a text-only model, providers like
    DeepSeek reject old `image_url` payloads. Keep the dialog shape but replace
    binary/image parts with a short text marker, matching Telegram's behavior.
    """
    sanitized: list[ModelMessage] = []
    replaced = 0
    for msg in history:
        if not isinstance(msg, ModelRequest):
            sanitized.append(msg)
            continue

        changed = False
        parts = []
        for part in msg.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, list):
                content = []
                for item in part.content:
                    if isinstance(item, MULTI_MODAL_CONTENT_TYPES):
                        content.append("[multimodal content omitted from text-model history]")
                        changed = True
                        replaced += 1
                    else:
                        content.append(item)
                parts.append(UserPromptPart(content=content, timestamp=part.timestamp))
            elif isinstance(part, ToolReturnPart) and part.has_content and isinstance(
                part.content,
                MULTI_MODAL_CONTENT_TYPES,
            ):
                changed = True
                replaced += 1
                parts.append(
                    ToolReturnPart(
                        tool_name=part.tool_name,
                        content="[multimodal tool content omitted from text-model history]",
                        tool_call_id=part.tool_call_id,
                        metadata=part.metadata,
                        timestamp=part.timestamp,
                        outcome=part.outcome,
                    )
                )
            else:
                parts.append(part)
        sanitized.append(ModelRequest(parts=parts) if changed else msg)
    if replaced:
        logger.debug(f"Discord history sanitized: replaced {replaced} multimodal items")
    return sanitized


def _discord_artwork_view(source_url: str, original_url: str | None = None) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Chi tiết", url=source_url))
    if original_url:
        view.add_item(discord.ui.Button(label="Ảnh gốc", url=original_url))
    return view


def _discord_artwork_embed(
    *,
    title: str,
    source_url: str,
    image_url: str,
    r18: bool,
    description: str | None = None,
    index: int | None = None,
) -> discord.Embed:
    display_title = f"🔞 {title}" if r18 else title
    if index is not None:
        display_title = f"{display_title} ({index})"
    embed = discord.Embed(
        title=display_title[:256],
        url=source_url,
        description=(description or "")[:3500] or None,
        color=0xFF8AC5 if not r18 else 0xFF5C8A,
    )
    embed.set_image(url=image_url)
    embed.set_footer(text="ManyACG / Pixiv")
    return embed


async def _fetch_discord_anime_artwork(
    keyword: str = "",
    r18_mode: int = 0,
) -> tuple[manyacg_service.Artwork, manyacg_service.Picture] | None:
    if manyacg_client is None:
        return None
    r18_mode = max(0, min(2, int(r18_mode)))
    try:
        if keyword:
            logger.debug(
                "Discord anime fetch: "
                f"keyword={keyword!r} r18_mode={r18_mode}({_r18_mode_label(r18_mode)})"
            )
            resp = await manyacg_client.client.get(
                "/artwork/list",
                params={
                    "r18": r18_mode,
                    "hybrid": app_config.manyacg_hybrid_search,
                    "keyword": keyword,
                },
            )
            if resp.status_code != 200:
                logger.error(
                    "Discord anime photo API returned "
                    f"{resp.status_code}: {resp.text[:300]!r}"
                )
                return None
            resp_model = manyacg_service.RandomArtworkResponse.model_validate(
                resp.json()
            )
        else:
            logger.debug(
                "Discord anime fetch random: "
                f"r18_mode={r18_mode}({_r18_mode_label(r18_mode)})"
            )
            resp_model = await manyacg_client.random_artwork(limit=1, r18=r18_mode)
        if resp_model.status != 200 or not resp_model.data:
            logger.error(
                "Discord anime photo API failed: "
                f"status={resp_model.status} message={resp_model.message!r} "
                f"r18_mode={r18_mode}({_r18_mode_label(r18_mode)})"
            )
            return None
        artwork = random.choice(resp_model.data)
        if not artwork.pictures:
            logger.error(
                "Discord anime fetch returned artwork without pictures: "
                f"title={artwork.title!r} r18={artwork.r18}"
            )
            return None
        logger.debug(
            "Discord anime fetch success: "
            f"title={artwork.title!r} r18={artwork.r18} "
            f"pictures={len(artwork.pictures)} r18_mode={r18_mode}"
        )
        return artwork, random.choice(artwork.pictures)
    except Exception as e:
        logger.error(f"Discord anime photo fetch error: {e.__class__.__name__}: {e}")
        return None


async def _send_discord_image_embed(
    message: discord.Message,
    embed: discord.Embed,
    image_url: str,
    view: discord.ui.View | None = None,
    *,
    spoiler: bool = False,
    channel: discord.abc.Messageable | None = None,
    content: str | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
    reference: discord.Message | None = None,
) -> bool:
    target_channel = channel or message.channel
    if not spoiler:
        embed.set_image(url=image_url)
        await target_channel.send(
            content=content,
            embed=embed,
            view=view,
            reference=reference,
            allowed_mentions=allowed_mentions,
            mention_author=False,
        )
        return True

    downloaded = await _download_image_bytes(image_url)
    if downloaded is None:
        logger.warning("Discord R18 spoiler image download failed; not exposing image URL")
        return False

    data, content_type = downloaded
    filename = "SPOILER_" + _image_filename(content_type)
    file = discord.File(io.BytesIO(data), filename=filename, spoiler=True)
    await target_channel.send(
        content=content,
        file=file,
        view=view,
        reference=reference,
        allowed_mentions=allowed_mentions,
        mention_author=False,
    )
    return True


async def _send_discord_anime_photo_card(
    message: discord.Message,
    artwork: manyacg_service.Artwork,
    picture: manyacg_service.Picture,
    *,
    channel: discord.abc.Messageable | None = None,
    content: str | None = None,
    caption: str | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
    reference: discord.Message | None = None,
) -> bool:
    original_url = f"https://t.me/{app_config.manyacg_bot}/?start=file_{picture.id}"
    embed = _discord_artwork_embed(
        title=artwork.title,
        source_url=artwork.source_url,
        image_url="",
        r18=artwork.r18,
    )
    caption_text = (caption or "").strip()
    if caption_text:
        embed.description = caption_text[:4096]
    success = await _send_discord_image_embed(
        message,
        embed,
        picture.regular,
        _discord_artwork_view(artwork.source_url, original_url),
        spoiler=artwork.r18,
        channel=channel,
        content=content,
        allowed_mentions=allowed_mentions,
        reference=reference,
    )
    if success:
        logger.info(
            "Discord anime send success: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"title={artwork.title!r} r18={artwork.r18} spoiler={artwork.r18}"
        )
    else:
        logger.error(
            "Discord anime send failed: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"title={artwork.title!r} r18={artwork.r18}"
        )
    return success


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


async def get_discord_server_info(
    ctx: RunContext[DiscordContextDeps], channel_limit: int = 25
) -> DiscordServerInfoResult:
    """Get information about the current Discord server and visible channels.

    Use this when the user asks about the Discord server, current channel,
    available channels, or general server context.

    Args:
        channel_limit: Maximum number of visible channels to return.
    """
    message = ctx.deps.message
    guild = message.guild
    if guild is None:
        return DiscordServerInfoResult(
            success=False,
            message="This is a DM, so there is no Discord server to inspect.",
        )

    limit = max(1, min(channel_limit, 50))
    channels: list[DiscordChannelInfo] = []
    guild_channels = sorted(
        guild.channels,
        key=lambda channel: (
            getattr(channel, "position", 0),
            getattr(channel, "name", ""),
        ),
    )
    for channel in guild_channels:
        if not _can_view_channel(channel, guild):
            continue
        channels.append(_channel_info(channel))
        if len(channels) >= limit:
            break

    owner_id = guild.owner_id
    return DiscordServerInfoResult(
        success=True,
        data=DiscordServerInfo(
            id=guild.id,
            name=guild.name,
            member_count=guild.member_count,
            owner_id=owner_id,
            owner_mention=f"<@{owner_id}>" if owner_id else None,
            current_channel=_channel_info(message.channel),
            channels=channels,
        ),
    )


async def find_discord_channel(
    ctx: RunContext[DiscordContextDeps], query: str, limit: int = 5
) -> DiscordServerInfoResult:
    """Find visible Discord channels in the current server.

    Use this before cross-channel scheduling when the user names a channel by
    mention, ID, or fuzzy name such as "thông báo" or "chat". If multiple
    channels match, ask the user to confirm which returned channel to use.

    Args:
        query: Channel mention, ID, or name text.
        limit: Maximum number of matching channels to return.
    """
    message = ctx.deps.message
    guild = message.guild
    if guild is None:
        return DiscordServerInfoResult(success=False, message="This is a DM, so there are no server channels.")
    normalized = query.strip().casefold().lstrip("#")
    query_id = _extract_discord_id(query)
    max_results = max(1, min(limit, 10))
    matches: list[DiscordChannelInfo] = []
    for channel in guild.channels:
        if not _can_view_channel(channel, guild):
            continue
        channel_id = getattr(channel, "id", 0)
        name = str(getattr(channel, "name", ""))
        if query_id is not None:
            is_match = channel_id == query_id
        elif not normalized:
            is_match = True
        else:
            name_folded = name.casefold()
            is_match = normalized == name_folded or normalized in name_folded
        if is_match:
            matches.append(_channel_info(channel))
            if len(matches) >= max_results:
                break
    if not matches:
        return DiscordServerInfoResult(success=False, message=f"No Discord channels found for query: {query!r}.")
    return DiscordServerInfoResult(
        success=True,
        data=DiscordServerInfo(
            id=guild.id,
            name=guild.name,
            member_count=guild.member_count,
            owner_id=guild.owner_id,
            owner_mention=f"<@{guild.owner_id}>" if guild.owner_id else None,
            current_channel=_channel_info(message.channel),
            channels=matches,
        ),
    )


async def find_discord_user(
    ctx: RunContext[DiscordContextDeps], query: str, limit: int = 5
) -> DiscordUserSearchResult:
    """Find Discord users in the current server/chat.

    Query may be a username, display name, server nickname, global name, mention,
    or numeric Discord user ID. Returns mention strings the AI can use to tag
    matching users.

    Args:
        query: User search text, mention, or user ID.
        limit: Maximum number of users to return.
    """
    users = await _find_discord_users(ctx.deps.message, query, limit)
    if not users:
        return DiscordUserSearchResult(
            success=False,
            message=f"No Discord users found for query: {query!r}.",
            users=[],
        )
    return DiscordUserSearchResult(
        success=True,
        users=[_discord_user_info(user, matched_by=query) for user in users],
    )


async def mention_discord_user(
    ctx: RunContext[DiscordContextDeps], query: str
) -> DiscordMentionResult:
    """Resolve a Discord user and return a mention string like <@123>.

    Use this when the user asks Waku to tag, ping, call, mention, or notify a
    specific Discord user. Put the returned `mention` directly in the final reply.

    Args:
        query: Username, display name, nickname, mention, or Discord user ID.
    """
    users = await _find_discord_users(ctx.deps.message, query, limit=1)
    if not users:
        return DiscordMentionResult(
            success=False,
            message=f"Could not resolve a Discord user for query: {query!r}.",
        )
    user_info = _discord_user_info(users[0], matched_by=query)
    return DiscordMentionResult(
        success=True,
        mention=user_info.mention,
        user=user_info,
    )


async def search_discord_messages(
    ctx: RunContext[DiscordContextDeps],
    query: str,
    limit: int = 100,
    max_results: int = 8,
    channel_id: int | None = None,
) -> DiscordMessageSearchResult:
    """Search recent readable Discord messages.

    Searches the current channel by default. Only searches channels the bot can
    read and where it has Read Message History permission.

    Args:
        query: Text to search for. Case-insensitive substring match.
        limit: Number of recent messages to scan, capped for safety.
        max_results: Maximum matching snippets to return.
        channel_id: Optional Discord channel ID. Defaults to current channel.
    """
    message = ctx.deps.message
    guild = message.guild
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return DiscordMessageSearchResult(
            success=False,
            message="Search query is empty.",
        )

    channel = await _resolve_discord_channel(message, channel_id)
    if channel is None:
        return DiscordMessageSearchResult(
            success=False,
            message="Could not find that Discord channel.",
        )
    if not _can_read_message_history(channel, guild):
        return DiscordMessageSearchResult(
            success=False,
            message="I do not have permission to read message history in that channel.",
        )
    history = getattr(channel, "history", None)
    if not callable(history):
        return DiscordMessageSearchResult(
            success=False,
            message="That Discord channel does not support message history search.",
        )

    scan_limit = max(1, min(limit, 300))
    result_limit = max(1, min(max_results, 20))
    matches: list[DiscordMessageSearchEntry] = []
    searched = 0
    try:
        async for previous_message in history(limit=scan_limit):
            searched += 1
            content = _message_text(previous_message)
            if not content or normalized_query not in content.casefold():
                continue
            matches.append(
                DiscordMessageSearchEntry(
                    message_id=previous_message.id,
                    channel_id=previous_message.channel.id,
                    channel_name=_channel_name(previous_message),
                    author_id=previous_message.author.id,
                    author_name=_author_name(previous_message),
                    author_mention=previous_message.author.mention,
                    created_at=previous_message.created_at.isoformat(),
                    content=content[:800],
                    jump_url=previous_message.jump_url,
                )
            )
            if len(matches) >= result_limit:
                break
    except discord.Forbidden:
        return DiscordMessageSearchResult(
            success=False,
            message="Discord denied access to that channel history.",
            searched=searched,
        )
    except Exception as e:
        logger.error(f"Discord message search error: {e.__class__.__name__}: {e}")
        return DiscordMessageSearchResult(
            success=False,
            message="Discord message search failed.",
            searched=searched,
        )

    return DiscordMessageSearchResult(
        success=True,
        message=None if matches else "No matching recent messages found.",
        searched=searched,
        matches=matches,
    )


async def _search_web_images(query: str, max_results: int = 8) -> list[dict]:
    def _search() -> list[dict]:
        with DDGS() as ddgs:
            return list(ddgs.images(query, max_results=max_results, safesearch="moderate"))

    return await asyncio.to_thread(_search)


async def _download_image_bytes(url: str) -> tuple[bytes, str] | None:
    max_bytes = 8_000_000
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "WakuDiscordBot/1.0"}) as response:
            if response.status_code >= 400:
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                return None
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    return None
            return bytes(data), content_type


def _image_filename(content_type: str) -> str:
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(content_type, "jpg")
    return f"waku_image.{extension}"


async def send_discord_web_image(
    ctx: RunContext[DiscordContextDeps], query: str
) -> DiscordWebImageResult:
    """Search the web for an image and upload it to the current Discord channel.

    Use this when the user asks for a general internet/web image that is not
    specifically an anime/Pixiv/setu image. This uploads the image to Discord.

    Args:
        query: Image search query.
    """
    message = ctx.deps.message
    search_query = query.strip()
    if not search_query:
        return DiscordWebImageResult(success=False, message="Image search query is empty.")
    r18_mode = await _discord_r18_mode(message.guild)
    if r18_mode == 0 and _contains_r18_keyword(search_query):
        return DiscordWebImageResult(
            success=False,
            message="R18 web image search is disabled in this Discord server.",
        )

    try:
        results = await _search_web_images(search_query)
    except Exception as e:
        logger.error(f"Discord web image search error: {e.__class__.__name__}: {e}")
        return DiscordWebImageResult(success=False, message="Web image search failed.")

    for result in results:
        image_url = result.get("image") or result.get("thumbnail")
        if not image_url:
            continue
        title = str(result.get("title") or search_query)
        source_url = str(result.get("url") or image_url)
        if r18_mode == 0 and _contains_r18_keyword(" ".join([title, source_url, image_url])):
            continue
        try:
            downloaded = await _download_image_bytes(str(image_url))
        except Exception as e:
            logger.debug(f"Discord web image download skipped: {e.__class__.__name__}: {e}")
            continue
        if downloaded is None:
            continue
        data, content_type = downloaded
        file = discord.File(io.BytesIO(data), filename=_image_filename(content_type))
        embed = discord.Embed(
            title=title[:256],
            url=source_url,
            description=f"Web image search: `{search_query[:120]}`",
            color=0x8AC5FF,
        )
        embed.set_image(url=f"attachment://{file.filename}")
        await message.channel.send(embed=embed, file=file, reference=message)
        return DiscordWebImageResult(
            success=True,
            title=title,
            source_url=source_url,
        )

    return DiscordWebImageResult(
        success=False,
        message="Could not find a downloadable image result.",
    )


async def send_discord_anime_photo(
    ctx: RunContext[DiscordContextDeps],
    keyword: str = "",
    target_channel_id: int | None = None,
    target_user_ids: list[int] | None = None,
    caption: str | None = None,
    allow_everyone: bool = False,
    count: int = 1,
) -> DiscordAnimePhotoResult:
    """Get and send an anime/Pixiv image to the current Discord chat.

    Call this tool when the user naturally asks Waku to send/show/give an
    anime/Pixiv image, picture, photo, setu, ảnh, hình, or similar. The current
    Discord server R18 mode is applied inside this tool as an API filter only.
    If this tool returns success=False, tell the user the image could not be
    sent instead of claiming that an image was sent.

    Args:
        keyword: Optional keyword to search for specific anime/Pixiv images.
        target_channel_id: Optional target channel ID for immediate cross-channel sends.
            Bot-admin-only when different from the current channel.
        target_user_ids: Discord user IDs to mention in the sent image message.
            Use when the user asks to gift/call/tag someone for the image.
        caption: Optional agent-written text to include with the image. For gift
            requests, write a direct sentence addressed to the recipient.
        allow_everyone: Bot-admin-only permission to allow @everyone/@here mentions.
        count: Number of images requested for this turn. The backend caps this to
            a safe batch size and spaces each send to avoid Discord limits.
    """
    message = ctx.deps.message
    target_channel = await _resolve_discord_channel(message, target_channel_id)
    if target_channel is None or not isinstance(target_channel, discord.abc.Messageable):
        return DiscordAnimePhotoResult(success=False, message="Target channel is not messageable or was not found.")
    target_channel_actual_id = getattr(target_channel, "id", message.channel.id)
    is_admin = _is_discord_bot_admin(message)
    if target_channel_actual_id != message.channel.id and not is_admin:
        return DiscordAnimePhotoResult(success=False, message="Only bot admins can send images in another channel.")
    if allow_everyone and not is_admin:
        return DiscordAnimePhotoResult(success=False, message="Only bot admins can send @everyone/@here mentions.")

    caption_text = (caption or "").strip()
    if ("@everyone" in caption_text or "@here" in caption_text) and not is_admin:
        return DiscordAnimePhotoResult(success=False, message="Only bot admins can send @everyone/@here mentions.")
    target_ids = list(target_user_ids or [])
    bot_user_id = _discord_client.user.id if _discord_client and _discord_client.user else None
    if caption_text:
        target_ids.extend(int(user_id) for user_id in re.findall(r"<@!?(\d+)>", caption_text))
    if not target_ids:
        target_ids = [
            user.id
            for user in message.mentions
            if not user.bot and user.id != bot_user_id
        ]
    target_ids = [user_id for user_id in dict.fromkeys(target_ids) if user_id != bot_user_id]
    mentions = [f"<@{user_id}>" for user_id in target_ids if user_id > 0]
    caption_has_mention = any(mention in caption_text for mention in mentions)
    message_content = None
    embed_caption = caption_text
    has_everyone_mention = "@everyone" in caption_text or "@here" in caption_text
    if mentions and caption_text:
        message_content = caption_text if caption_has_mention else f"{' '.join(mentions)} {caption_text}"
        embed_caption = ""
    elif has_everyone_mention and caption_text:
        message_content = caption_text
        embed_caption = ""
    elif mentions:
        message_content = " ".join(mentions)

    r18_mode = await _discord_r18_mode(message.guild)
    if r18_mode == 0 and _contains_r18_keyword(keyword):
        return DiscordAnimePhotoResult(
            success=False,
            message="R18 image sending is disabled in this Discord server.",
        )
    if manyacg_client is None:
        return DiscordAnimePhotoResult(
            success=False,
            message="ManyACG is not configured, so Discord cannot send anime photos.",
        )

    requested_count = max(1, int(count or 1))
    capped_count = min(requested_count, _DISCORD_IMAGE_BATCH_MAX)
    if requested_count > _DISCORD_IMAGE_BATCH_MAX:
        logger.info(
            "discord_image_batch_capped "
            f"user={message.author.id} channel={target_channel_actual_id} "
            f"requested={requested_count} capped={capped_count}"
        )

    image_lock = _discord_image_lock()
    if image_lock.locked():
        logger.info(
            "discord_image_queue_busy "
            f"user={message.author.id} channel={target_channel_actual_id} wait={_DISCORD_IMAGE_BUSY_WAIT_SECONDS}"
        )
        return DiscordAnimePhotoResult(
            success=False,
            message="Discord image sender is busy. Ask the user to wait a few seconds; the image will be available soon.",
            requested_count=requested_count,
            capped_count=capped_count,
            wait_seconds=_DISCORD_IMAGE_BUSY_WAIT_SECONDS,
        )

    sent_count = 0
    last_info: DiscordAnimePhotoInfo | None = None
    async with image_lock:
        for index in range(capped_count):
            logger.info(
                "discord_image_request_start "
                f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
                f"target_channel={target_channel_actual_id} user={message.author.id} "
                f"keyword={keyword!r} index={index + 1}/{capped_count}"
            )
            fetched = await _fetch_discord_anime_artwork(keyword.strip(), r18_mode=r18_mode)
            if fetched is None:
                logger.warning(
                    "discord_image_fetch_failed "
                    f"user={message.author.id} channel={target_channel_actual_id} keyword={keyword!r} "
                    f"index={index + 1}/{capped_count}"
                )
                if sent_count == 0:
                    return DiscordAnimePhotoResult(
                        success=False,
                        message="Failed to fetch anime artwork from ManyACG.",
                        sent_count=sent_count,
                        requested_count=requested_count,
                        capped_count=capped_count,
                    )
                break
            artwork, picture = fetched
            logger.info(
                "discord_image_fetch_success "
                f"user={message.author.id} channel={target_channel_actual_id} "
                f"title={artwork.title!r} index={index + 1}/{capped_count}"
            )
            if not await _send_discord_anime_photo_card(
                message,
                artwork,
                picture,
                channel=target_channel,
                content=message_content,
                caption=embed_caption,
                allowed_mentions=_discord_allowed_mentions(allow_everyone),
                reference=message if target_channel_actual_id == message.channel.id and index == 0 else None,
            ):
                logger.warning(
                    "discord_image_send_failed "
                    f"user={message.author.id} channel={target_channel_actual_id} "
                    f"title={artwork.title!r} index={index + 1}/{capped_count}"
                )
                if sent_count == 0:
                    return DiscordAnimePhotoResult(
                        success=False,
                        message="Failed to upload the anime artwork to Discord.",
                        sent_count=sent_count,
                        requested_count=requested_count,
                        capped_count=capped_count,
                    )
                break
            sent_count += 1
            artist = None
            if artwork.artist is not None:
                artist = DiscordArtistInfo(
                    name=artwork.artist.name,
                    type=artwork.artist.type,
                    username=artwork.artist.username,
                    uid=artwork.artist.uid,
                )
            last_info = DiscordAnimePhotoInfo(
                title=artwork.title,
                source_url=artwork.source_url,
                r18=artwork.r18,
                description=artwork.description[:512],
                artist=artist,
                tags=artwork.tags[:10],
            )
            if index < capped_count - 1:
                await asyncio.sleep(_DISCORD_IMAGE_SEND_DELAY_SECONDS)
    logger.info(
        "discord_image_batch_done "
        f"user={message.author.id} channel={target_channel_actual_id} "
        f"sent={sent_count} requested={requested_count} capped={capped_count}"
    )
    result_message = None
    if requested_count > capped_count:
        result_message = f"Sent {sent_count}/{requested_count} requested images. For Discord safety, Waku can only send up to {capped_count} images per batch; ask again if you want more."
    elif sent_count > 1:
        result_message = f"Sent {sent_count} images safely with a short delay between each one."
    return DiscordAnimePhotoResult(
        success=sent_count > 0,
        message=result_message,
        data=last_info,
        sent_count=sent_count,
        requested_count=requested_count,
        capped_count=capped_count,
    )


async def _send_discord_setu(message: discord.Message) -> bool:
    if manyacg_client is None:
        await message.channel.send("ManyACG is not configured, so Waku cannot send images yet.", reference=message)
        return True

    ratekey = f"discord_setu_cd:{message.channel.id}:{message.author.id}"
    if await common.memttlcache.get(ratekey, False):
        await message.channel.send("Please wait a moment before requesting another image.", reference=message)
        return True
    await common.memttlcache.set(ratekey, True, ttl=app_config.manyacg_setu_cd)

    image_lock = _discord_image_lock()
    if image_lock.locked():
        logger.info(f"discord_image_queue_busy setu user={message.author.id} channel={message.channel.id}")
        await message.channel.send("Waku đang xử lý ảnh khác, đợi vài giây rồi gọi lại nha.", reference=message)
        return True

    try:
        r18_mode = await _discord_r18_mode(message.guild)
        async with message.channel.typing():
            async with image_lock:
                fetched = await _fetch_discord_anime_artwork(r18_mode=r18_mode)
                if fetched is None:
                    await message.channel.send("Could not fetch an image from ManyACG.", reference=message)
                    return True
                artwork, picture = fetched
                await _send_discord_anime_photo_card(message, artwork, picture)
            return True
    except Exception as e:
        logger.error(f"Discord setu error: {e.__class__.__name__}: {e}")
        await message.channel.send("Image sending failed. Please try again later.", reference=message)
        return True


async def _send_discord_artwork(message: discord.Message, artwork_url: str) -> bool:
    if manyacg_client is None:
        await message.channel.send("ManyACG is not configured, so Waku cannot parse artwork links yet.", reference=message)
        return True
    if not app_config.manyacg_api_key:
        await message.channel.send("ManyACG API key is missing, so Waku cannot parse artwork links yet.", reference=message)
        return True

    try:
        async with message.channel.typing():
            resp = await manyacg_client.fetch_artwork(artwork_url)
        if resp.status != 200 or resp.data is None:
            await message.channel.send("Could not fetch artwork from this link.", reference=message)
            return True
        artwork = resp.data
        if artwork.r18 and await _discord_r18_mode(message.guild) == 0:
            await message.channel.send("R18 artwork is disabled in this Discord server.", reference=message)
            return True
        pictures = sorted(artwork.pictures or [], key=lambda item: item.index)
        if not pictures:
            await message.channel.send("This artwork has no image to send.", reference=message)
            return True
        sent = False
        view = _discord_artwork_view(artwork.source_url)
        for index, picture in enumerate(pictures[:4], start=1):
            embed = _discord_artwork_embed(
                title=artwork.title,
                source_url=artwork.source_url,
                image_url="",
                r18=artwork.r18,
                description=artwork.description if index == 1 else None,
                index=index if len(pictures) > 1 else None,
            )
            if index == 1 and len(pictures) > 4:
                embed.add_field(
                    name="More images",
                    value=f"This artwork has {len(pictures)} images; sending the first 4.",
                    inline=False,
                )
            success = await _send_discord_image_embed(
                message,
                embed,
                picture.original,
                view if not sent else None,
                spoiler=artwork.r18,
            )
            if not success:
                await message.channel.send(
                    "Failed to upload this R18 artwork as a spoiler attachment.",
                    reference=message,
                )
                return True
            sent = True
        return True
    except Exception as e:
        logger.error(f"Discord artwork parse error: {e.__class__.__name__}: {e}")
        await message.channel.send("Failed to parse this artwork link.", reference=message)
        return True


class DiscordConfigView(discord.ui.View):
    def __init__(self, guild: discord.Guild, settings: DiscordGuildSettings):
        super().__init__(timeout=300)
        self.guild = guild
        self.message: discord.Message | None = None
        self.pending_settings = DiscordGuildSettings(
            enabled=settings.enabled,
            r18_mode=settings.r18_mode,
            ai_reply=settings.ai_reply,
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
            r18_button.label = f"R18: {_r18_mode_label(self.pending_settings.r18_mode)}"
            r18_button.style = (
                discord.ButtonStyle.danger
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

    @discord.ui.button(label="R18", style=discord.ButtonStyle.secondary)
    async def toggle_r18(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.pending_settings.r18_mode = (self.pending_settings.r18_mode + 1) % 3
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
            f"({_r18_mode_label(self.pending_settings.r18_mode)}) "
            f"ai_reply={self.pending_settings.ai_reply}"
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

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success)
    async def save_config(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await _set_discord_dm_settings(self.user, self.pending_settings)
        logger.info(
            "Discord DM config saved: "
            f"user={self.user.id} ai_reply={self.pending_settings.ai_reply}"
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
    return (
        "**Waku DM config:**\n"
        f"AI Reply: `{'ON' if settings.ai_reply else 'OFF'}`\n"
        "\nWhen AI Reply is `OFF`, Waku will ignore normal DM chat messages. "
        "Commands like `!config` still work.\n"
        "\nPress `Save` to apply changes."
    )


def _discord_config_text(settings: DiscordGuildSettings) -> str:
    return (
        "**Waku Bot Server config:**\n"
        "Server: `Authorized!`\n"
        f"AI Reply: `{'ON' if settings.ai_reply else 'OFF'}`\n"
        f"R18 images: `{_r18_mode_label(settings.r18_mode)}`\n"
        "\nPress `Save` to apply changes."
    )


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
        guild = _discord_client.get_guild(guild_id) if _discord_client else None
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


async def _handle_discord_admin_command(message: discord.Message) -> bool:
    prefix = app_config.discord_command_prefix or "!"
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
            guild = _discord_client.get_guild(guild_id) if _discord_client else None
            if command == "waku":
                await _set_discord_guild_settings_by_id(
                    guild_id,
                    guild.name if guild else None,
                    DiscordGuildSettings(enabled=True, r18_mode=0, ai_reply=True),
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
    if _is_setu_command(content):
        logger.info(
            f"Discord command setu request: guild={_guild_name(message)!r} "
            f"channel={_channel_name(message)!r} user={message.author.id}"
        )
        return await _send_discord_setu(message)
    return False


async def _handle_message(message: discord.Message, user_prompt: str) -> None:
    if _discord_agent is None:
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

    await common.memstore.set(waiting_key, True)
    try:
        async with message.channel.typing():
            result = await _discord_agent.run(
                user_prompt=prompt,
                message_history=model_history[-app_config.discord_message_history_limit :],
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
    except Exception as e:
        logger.error(f"Discord agent error: {e.__class__.__name__}: {e}")
        await message.channel.send("AI đang bị lỗi nhẹ, thử lại sau nha.", reference=message)
    finally:
        await common.memstore.delete(waiting_key)


def _create_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True
    intents.members = app_config.discord_members_intent
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        user = client.user
        if user is None:
            logger.warning("Discord client ready without user")
            return
        logger.success(f"Discord AI chat ready as {user} ({user.id})")
        global _server_list_view_registered
        if not _server_list_view_registered:
            client.add_view(DiscordServerListView())
            _server_list_view_registered = True
            logger.info("Discord persistent server list view registered")

    @client.event
    async def on_message(message: discord.Message) -> None:
        bot_user = client.user
        if bot_user is None or message.author.bot:
            return
        content = _message_text(message)
        if await _handle_discord_admin_command(message):
            return
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
            f"setu_command={_is_setu_command(content)} "
            f"allowed={await _channel_allowed(message)}"
        )
        if await _maybe_handle_discord_media_request(message):
            return
        await _handle_message(message, prompt)

    return client


async def start_discord_bot() -> None:
    global _discord_agent, _discord_client, _discord_task

    if not app_config.discord_enabled:
        logger.debug("Discord AI chat disabled")
        return
    if not app_config.discord_token:
        logger.warning("Discord AI chat enabled but discord_token is empty")
        return
    if not app_config.agent or not app_config.agent_model:
        logger.warning("Discord AI chat requires agent=true and agent_model")
        return

    _discord_agent = Agent(
        model=provider.make_chat_model(app_config.agent_model),
        instructions=(
            f"{app_config.agent_group_prompt or app_config.agent_prompt}\n\n"
            "Discord style: keep Waku's cute, playful chat style. "
            "Use natural emojis/emoticons in most casual replies, usually 1-3, "
            "but do not spam them or add them to serious/admin/error messages. "
            "Discord image behavior: when the user asks Waku to send/show/give "
            "a new anime/Pixiv image, photo, picture, setu, ảnh, or hình, call "
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
            "the user asks for a future time, delay, or repetition. For general "
            "internet/web image requests, use send_discord_web_image with a "
            "concise search query. "
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
            "messages. Only search chat when the user asks or it clearly helps. "
            "Discord reaction behavior: Waku may call send_discord_reaction "
            "when a lightweight reaction fits better than a text reply, or as a "
            "small addition to a short reply. Prefer learned emojis from Discord "
            "reaction style context, but default to common Unicode reactions like "
            "👍 ❤️ 😂 😭 🔥 🎉 👏 👀 🤔 🥰 😮 🙏. Do not overuse reactions. "
            "If send_discord_reaction fails, do not claim a reaction was added. "
            "If a Discord permission is missing, explain that briefly."
        ),
        output_type=str,
        tools=[
            Tool(get_discord_server_info, sequential=True),
            Tool(find_discord_channel, sequential=True),
            Tool(find_discord_user, sequential=True),
            Tool(mention_discord_user, sequential=True),
            Tool(search_discord_messages, sequential=True),
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
    _discord_client = _create_client()
    _discord_task = asyncio.create_task(_discord_client.start(app_config.discord_token))
    logger.info("Discord AI chat startup scheduled")


async def stop_discord_bot() -> None:
    global _discord_agent, _discord_client, _discord_task

    if _discord_client is not None:
        await _discord_client.close()
    if _discord_task is not None:
        try:
            await _discord_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Discord client stopped with error: {e.__class__.__name__}: {e}")
    _discord_task = None
    _discord_client = None
    _discord_agent = None
