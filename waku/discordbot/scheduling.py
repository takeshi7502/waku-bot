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
from .media import _discord_artwork_embed, _download_image_bytes, _fetch_discord_anime_artwork, _image_filename, _search_web_images
from .messages import _resolve_discord_channel
from .models import DiscordContextDeps, DiscordScheduleResult, _DiscordScheduledJob
from .permissions import _discord_allowed_mentions, _is_discord_bot_admin
from .settings import _discord_r18_mode
from .state import _discord_image_lock

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
    if state.discord_client is None:
        logger.error("Scheduled Discord message failed: Discord client unavailable")
        return
    channel = state.discord_client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await state.discord_client.fetch_channel(channel_id)
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
    if state.discord_client is None:
        logger.error("Scheduled Discord image failed: Discord client unavailable")
        return
    channel = state.discord_client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await state.discord_client.fetch_channel(channel_id)
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
    bot_user_id = state.discord_client.user.id if state.discord_client and state.discord_client.user else None
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
    bot_user_id = state.discord_client.user.id if state.discord_client and state.discord_client.user else None
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
