import datetime
import random
from dataclasses import dataclass
from hashlib import md5
from typing import Literal

import pyrogram
import pyrogram.errors
from pydantic_ai import ModelRetry, RunContext
from pyrogram.raw.functions.messages import SetBotGuestChatResult
from pyrogram.raw.types import (
    DocumentAttributeImageSize,
    InputBotInlineMessageMediaAuto,
    InputBotInlineResult,
    InputWebDocument,
)

from waku import common, database, i18n
from waku.bot.client import client
from waku.config import app_config
from waku.logger import logger
from waku.plugins.manyacg import manyacg

from .. import datatype, sticker_memory, sticker_vec


@dataclass
class SendResult:
    success: bool
    message: str | None = None

    def text(self) -> str:
        if self.success:
            msg = "发送成功"
            if self.message:
                msg = f"{msg}, 提示信息: {self.message}"
            return msg
        msg = "发送失败"
        if self.message:
            msg = f"{msg}, 错误信息: {self.message}"
        return msg


@dataclass
class _ScheduledAgentJob:
    job_id: str
    kind: str
    chat_id: int
    user_id: int
    run_time: datetime.datetime | None
    summary: str


_SCHEDULE_PREFIXES = {
    "agent_schedule_msg": "message",
    "agent_schedule_media": "media",
    "agent_send_poll": "poll",
}


def _clean_job_id(job_id: str) -> str:
    return job_id.removesuffix("_memory")


def _parse_scheduled_agent_job(job) -> _ScheduledAgentJob | None:
    job_id = _clean_job_id(str(job.id))
    parts = job_id.split(":")
    if len(parts) < 5 or parts[0] not in _SCHEDULE_PREFIXES:
        return None
    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        return None

    run_time = getattr(job, "next_run_time", None)
    args = list(getattr(job, "args", []) or [])
    kind = _SCHEDULE_PREFIXES[parts[0]]
    summary = kind
    if kind == "message" and len(args) >= 2:
        summary = str(args[1])[:120]
    elif kind == "media" and len(args) >= 3:
        caption = str(args[3])[:80] if len(args) >= 4 and args[3] else ""
        summary = f"{args[1]} {args[2]} {caption}".strip()[:120]
    elif kind == "poll" and len(args) >= 2:
        summary = f"poll: {args[1]}"[:120]

    return _ScheduledAgentJob(
        job_id=job_id,
        kind=kind,
        chat_id=chat_id,
        user_id=user_id,
        run_time=run_time,
        summary=summary,
    )


def _scheduled_agent_jobs(chat_id: int, user_id: int | None = None) -> list[_ScheduledAgentJob]:
    jobs: list[_ScheduledAgentJob] = []
    for job in common.jobqueue.get_all_jobs():
        parsed = _parse_scheduled_agent_job(job)
        if parsed is None or parsed.chat_id != chat_id:
            continue
        if user_id is not None and parsed.user_id != user_id:
            continue
        jobs.append(parsed)
    return sorted(jobs, key=lambda item: item.run_time or datetime.datetime.max.replace(tzinfo=datetime.UTC))


def _format_scheduled_job(job: _ScheduledAgentJob, index: int) -> str:
    when = job.run_time.isoformat() if job.run_time else "unknown time"
    return f"{index}. [{job.kind}] {when} - {job.summary} - id={job.job_id}"


# Module-level job functions for APScheduler persistence
# These are defined at module level so they can be serialized by reference


async def _scheduled_text_job(chat_id: int, text: str) -> None:
    """Module-level function to send scheduled text message.

    Args:
        chat_id: Target chat ID
        text: Message text to send
    """
    try:
        await client.send_message(chat_id=chat_id, text=text)
        logger.info("Scheduled text message sent successfully")
    except Exception as e:
        logger.error(f"Scheduled text message failed: {e.__class__.__name__}: {e}")


async def _scheduled_media_job(
    chat_id: int,
    media_type: Literal["photo", "video", "audio", "document"],
    media_url: str,
    caption: str,
) -> None:
    """Module-level function to send scheduled media message.

    Args:
        chat_id: Target chat ID
        media_type: Type of media
        media_url: Media URL
        caption: Media caption
    """
    try:
        match media_type:
            case "photo":
                await client.send_photo(
                    chat_id=chat_id,
                    photo=media_url,
                    caption=caption,
                )
            case "video":
                await client.send_video(
                    chat_id=chat_id,
                    video=media_url,
                    caption=caption,
                )
            case "audio":
                await client.send_audio(
                    chat_id=chat_id,
                    audio=media_url,
                    caption=caption,
                )
            case "document":
                await client.send_document(
                    chat_id=chat_id,
                    document=media_url,
                    caption=caption,
                )
        logger.info(f"Scheduled {media_type} message sent successfully")
    except Exception as e:
        logger.error(
            f"Scheduled {media_type} message failed: {e.__class__.__name__}: {e}"
        )


async def _scheduled_poll_job(
    chat_id: int,
    question: str,
    options: list[str],
    is_anonymous: bool,
    allows_multiple_answers: bool,
) -> None:
    """Module-level function to send scheduled poll.

    Args:
        chat_id: Target chat ID
        question: Poll question
        options: Poll options
        is_anonymous: Whether the poll is anonymous
        allows_multiple_answers: Whether multiple answers are allowed
    """
    try:
        from waku.bot.client import client

        await client.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=is_anonymous,
            allows_multiple_answers=allows_multiple_answers,
        )
        logger.info(f"Scheduled poll sent successfully: {question[:30]}...")
    except Exception as e:
        logger.error(f"Scheduled poll failed: {e.__class__.__name__}: {e}")


async def schedule_message(
    ctx: RunContext[datatype.ContextDeps],
    schedule_time: str | None,
    send_immediately: bool = False,
    text: str | None = None,
    media_type: Literal["photo", "video", "audio", "document"] | None = None,
    media_url: str | None = None,
    caption: str | None = None,
) -> str:
    """Schedule a message to be sent at a specific time.

    Use this tool to schedule delayed messages (text or media) for future delivery.

    Args:
        schedule_time: ISO 8601 datetime string for scheduled delivery,
            e.g. "2025-06-04T15:00:00+08:00". Must be in the future.
        send_immediately: If True, send the message immediately.
        text: The message text to send (for text messages). Either text or media
            must be provided, but not both.
        media_type: Media type for media messages. One of "photo", "video",
            "audio", "document". Required if media_url is provided.
        media_url: Direct URL for media types. Required if media_type is provided.
        caption: Optional caption for media messages.

    Returns:
        A SendResult indicating success or failure.
    """
    if ctx.deps.message is None or ctx.deps.chat_id is None:
        return SendResult(
            success=False, message="Message context is unavailable."
        ).text()
    if not send_immediately and not schedule_time:
        raise ModelRetry("Must provide either schedule_time or send_immediately=True")

    # Validate schedule_time if not sending immediately
    schedule_datetime: datetime.datetime | None = None
    if not send_immediately and schedule_time:
        try:
            schedule_datetime = datetime.datetime.fromisoformat(schedule_time)
        except ValueError as e:
            raise ModelRetry(
                f"Invalid schedule_time format. Use ISO 8601, e.g. '2025-06-04T15:00:00+08:00'. Error: {e}"
            )
        if schedule_datetime < datetime.datetime.now(datetime.UTC):
            raise ModelRetry("schedule_time must be in the future.")

    # Validate message content
    has_text = text is not None and text.strip()
    has_media = media_type is not None or media_url is not None

    if has_text and has_media:
        raise ModelRetry(
            "Cannot provide both text and media. Use caption for media description."
        )
    if not has_text and not has_media:
        raise ModelRetry("Must provide either text or media (media_type + media_url).")
    if has_media and not media_type:
        raise ModelRetry("media_type is required when providing media_url.")
    if has_media and not media_url:
        raise ModelRetry("media_url is required when providing media_type.")

    chat_id = ctx.deps.chat_id

    if send_immediately:
        # Send immediately without scheduling
        try:
            if has_text:
                assert text is not None
                await ctx.deps.client.send_message(chat_id=chat_id, text=text)
                return SendResult(success=True, message="Message sent.").text()
            else:
                # Send media immediately
                assert media_type is not None
                assert media_url is not None
                caption = caption if caption else ""
                match media_type:
                    case "photo":
                        await ctx.deps.client.send_photo(
                            chat_id=chat_id,
                            photo=media_url,
                            caption=caption,
                        )
                    case "video":
                        await ctx.deps.client.send_video(
                            chat_id=chat_id,
                            video=media_url,
                            caption=caption,
                        )
                    case "audio":
                        await ctx.deps.client.send_audio(
                            chat_id=chat_id,
                            audio=media_url,
                            caption=caption,
                        )
                    case "document":
                        await ctx.deps.client.send_document(
                            chat_id=chat_id,
                            document=media_url,
                            caption=caption,
                        )
                return SendResult(success=True, message=f"{media_type} sent.").text()
        except Exception as e:
            logger.error(f"Immediate send failed: {e.__class__.__name__}: {e}")
            return SendResult(success=False, message=f"Failed to send: {e}").text()
    else:
        # Schedule for later delivery
        # At this point schedule_datetime must be set (validated above)
        assert schedule_datetime is not None

        if has_text:
            # Schedule text message using module-level function
            assert text is not None
            text_content = text
            job_key = (
                f"agent_schedule_msg:{chat_id}:{ctx.deps.user_id}"
                f":{schedule_datetime.timestamp()}"
                f":{md5(text_content.encode()).hexdigest()}"
            )

            common.jobqueue.add_onetime_job(
                job_key,
                run_date=schedule_datetime,
                func=_scheduled_text_job,
                args=[chat_id, text_content],
            )
        else:
            # Schedule media message using module-level function
            assert media_type is not None
            assert media_url is not None
            _caption = caption if caption else ""
            job_key = (
                f"agent_schedule_media:{chat_id}:{ctx.deps.user_id}"
                f":{schedule_datetime.timestamp()}"
                f":{md5(media_url.encode()).hexdigest()}"
            )

            common.jobqueue.add_onetime_job(
                job_key,
                run_date=schedule_datetime,
                func=_scheduled_media_job,
                args=[chat_id, media_type, media_url, _caption],
            )

        return SendResult(
            success=True, message=f"Scheduled for {schedule_datetime.isoformat()}"
        ).text()


async def list_scheduled_messages(
    ctx: RunContext[datatype.ContextDeps],
    only_mine: bool = False,
) -> str:
    """List pending scheduled messages and polls in the current chat.

    Args:
        only_mine: If True, only list schedules created by the current user.

    Returns:
        A numbered list of pending schedule jobs with their job IDs.
    """
    if ctx.deps.chat_id is None:
        return SendResult(
            success=False, message="Message context is unavailable."
        ).text()
    user_id = ctx.deps.user_id if only_mine else None
    jobs = _scheduled_agent_jobs(ctx.deps.chat_id, user_id=user_id)
    if not jobs:
        return "Không có lịch hẹn nào trong chat này."
    lines = [_format_scheduled_job(job, index) for index, job in enumerate(jobs, start=1)]
    return "Lịch hẹn hiện tại:\n" + "\n".join(lines[:20])


async def cancel_scheduled_message(
    ctx: RunContext[datatype.ContextDeps],
    job_id: str | None = None,
    match_text: str | None = None,
    cancel_all: bool = False,
    only_mine: bool = False,
) -> str:
    """Cancel pending scheduled messages or polls in the current chat.

    Args:
        job_id: Exact schedule job ID from list_scheduled_messages.
        match_text: Text to match against job ID or summary when cancelling one or more jobs.
        cancel_all: If True, cancel all matching schedules in the current chat.
        only_mine: If True, only cancel schedules created by the current user.

    Returns:
        A SendResult describing what was cancelled.
    """
    if ctx.deps.chat_id is None:
        return SendResult(
            success=False, message="Message context is unavailable."
        ).text()

    user_id = ctx.deps.user_id if only_mine else None
    jobs = _scheduled_agent_jobs(ctx.deps.chat_id, user_id=user_id)
    if not jobs:
        return SendResult(success=False, message="Không có lịch hẹn nào để huỷ.").text()

    targets: list[_ScheduledAgentJob]
    if job_id:
        normalized_job_id = _clean_job_id(job_id.strip())
        targets = [job for job in jobs if job.job_id == normalized_job_id]
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
        return SendResult(
            success=False,
            message="Cần job_id, match_text hoặc cancel_all=True để huỷ lịch hẹn.",
        ).text()

    if not targets:
        return SendResult(success=False, message="Không tìm thấy lịch hẹn phù hợp.").text()
    if len(targets) > 1 and not cancel_all and not match_text:
        return SendResult(
            success=False,
            message="Tìm thấy nhiều lịch hẹn; hãy list rồi chọn job_id cụ thể.",
        ).text()

    cancelled: list[str] = []
    for job in targets:
        common.jobqueue.remove_job(job.job_id)
        cancelled.append(_format_scheduled_job(job, len(cancelled) + 1))

    return SendResult(
        success=True,
        message="Đã huỷ lịch hẹn:\n" + "\n".join(cancelled[:10]),
    ).text()


async def send_poll(
    ctx: RunContext[datatype.ContextDeps],
    question: str,
    options: list[str],
    is_anonymous: bool = False,
    allows_multiple_answers: bool = False,
    schedule_time: str | None = None,
) -> str:
    """Send a poll to the current chat.

    Use this tool to create interactive polls with 2-8 options for users to vote on.

    Args:
        question: The poll question text (1-300 characters).
        options: List of 2-8 answer options for the poll.
        is_anonymous: Whether the poll is anonymous.
        allows_multiple_answers: Whether users can select multiple answers.
        schedule_time: Optional ISO 8601 datetime string to schedule delivery,
            e.g. "2025-06-04T15:00:00+08:00". If omitted, sends immediately.

    Returns:
        A SendResult indicating success or failure.
    """
    if ctx.deps.message is None or ctx.deps.chat_id is None:
        return SendResult(
            success=False, message="Message context is unavailable."
        ).text()

    schedule_datetime: datetime.datetime | None = None
    if schedule_time is not None:
        try:
            schedule_datetime = datetime.datetime.fromisoformat(schedule_time)
        except ValueError as e:
            raise ModelRetry(
                f"Invalid schedule_time format. Use ISO 8601, e.g. '2025-06-04T15:00:00+08:00'. Error: {e}"
            )
        if schedule_datetime < datetime.datetime.now(datetime.UTC):
            raise ModelRetry("schedule_time must be in the future.")

    reply_params = pyrogram.types.ReplyParameters(
        message_id=ctx.deps.message.id,
    )
    chat_id = ctx.deps.chat_id

    if not question or not question.strip():
        raise ModelRetry("'question' is required for poll.")
    if not options or len(options) < 2:
        raise ModelRetry("'options' must have at least 2 items.")
    if len(options) > 10:
        raise ModelRetry("'options' must have at most 10 items.")

    if schedule_datetime is not None:
        await _schedule_poll(
            ctx,
            question,
            options,
            is_anonymous,
            allows_multiple_answers,
            schedule_datetime,
            chat_id,
        )
        return SendResult(
            success=True, message=f"Scheduled for {schedule_datetime.isoformat()}"
        ).text()

    try:
        await ctx.deps.client.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=is_anonymous,
            allows_multiple_answers=allows_multiple_answers,
            reply_parameters=reply_params,
        )
    except Exception as e:
        logger.error(f"send_poll failed: {e.__class__.__name__}: {e}")
        raise ModelRetry(f"Failed to send poll: {e.__class__.__name__}: {e}")

    return SendResult(success=True).text()


async def _schedule_poll(
    ctx: RunContext[datatype.ContextDeps],
    question: str,
    options: list[str],
    is_anonymous: bool,
    allows_multiple_answers: bool,
    schedule_datetime: datetime.datetime,
    chat_id: int,
) -> None:
    job_key = (
        f"agent_send_poll:{chat_id}:{ctx.deps.user_id}"
        f":{schedule_datetime.timestamp()}"
        f":{md5(question.encode()).hexdigest()}"
    )

    # Use module-level function for persistence
    common.jobqueue.add_onetime_job(
        job_key,
        run_date=schedule_datetime,
        func=_scheduled_poll_job,
        args=[chat_id, question, options, is_anonymous, allows_multiple_answers],
    )


async def send_sticker(
    ctx: RunContext[datatype.ContextDeps],
    query: str,
) -> str:
    """Search for a semantically matching sticker and send it.

    Args:
        query: Natural language description of the desired sticker, e.g. "happy excited",
               "sad crying", "thumbs up approval".

    Returns:
        A SendResult indicating success or failure.
    """
    if ctx.deps.chat_id is None or ctx.deps.message is None:
        return SendResult(
            success=False, message="Message context is unavailable."
        ).text()

    if sticker_memory.embedder is None and sticker_memory._embedding_agent is None:
        return SendResult(
            success=False, message="Sticker memory is not configured."
        ).text()

    embedding = await sticker_memory.get_embedding(query)
    if embedding is None:
        raise ModelRetry("Failed to embed query.")

    results = await sticker_vec.search(ctx.deps.chat_id, embedding, k=1)
    if not results:
        raise ModelRetry("No matching sticker found in this group's sticker memory.")

    file_id, description, distance = results[0]
    logger.debug(
        f"send_sticker: query={query!r} -> description={description!r} distance={distance:.4f}"
    )
    try:
        await ctx.deps.client.send_sticker(
            chat_id=ctx.deps.chat_id,
            sticker=file_id,
            reply_parameters=pyrogram.types.ReplyParameters(
                message_id=ctx.deps.message.id,
            ),
        )
        logger.debug(
            f"send_sticker success: chat_id={ctx.deps.chat_id} query={query!r} "
            f"distance={distance:.4f} description={description[:80]!r}"
        )
    except Exception as e:
        logger.error(
            f"send_sticker failed: chat_id={ctx.deps.chat_id} query={query!r} "
            f"distance={distance:.4f} error={e.__class__.__name__}: {e}"
        )
        raise ModelRetry(f"Failed to send sticker: {e.__class__.__name__}: {e}")
    # Mark as already called this turn so prepare_periodic_sticker suppresses
    # the "MUST call" hint for any further steps within the same agent run.
    ctx.deps.tools_called_this_turn.add("send_sticker")
    return SendResult(success=True).text()


async def send_reaction(
    ctx: RunContext[datatype.ContextDeps],
    emoji: str,
    target_message_id: int | None = None,
) -> str:
    """Add a Telegram reaction emoji to a message.

    Telegram only supports a limited set of reaction emojis, and each chat may
    allow only some of them. Prefer common Telegram reactions such as "👍",
    "❤️", "🔥", "🥰", "👏", "😁", "🤔", "😢", "😡", or "🎉".

    Args:
        emoji: The reaction emoji to use.
        target_message_id: Optional message ID to react to. If not provided,
            reacts to the current user's message.

    Returns:
        A SendResult indicating success or failure.
    """
    if ctx.deps.message is None:
        return SendResult(
            success=False, message="Message context is unavailable."
        ).text()

    # Use provided target message ID or default to current message
    message_id = (
        target_message_id if target_message_id is not None else ctx.deps.message.id
    )

    try:
        await ctx.deps.client.send_reaction(
            chat_id=ctx.deps.chat_id,
            message_id=message_id,
            emoji=emoji,
        )
    except pyrogram.errors.exceptions.bad_request_400.ReactionInvalid:
        chat = await common.get_chat_full(ctx.deps.client, ctx.deps.chat_id)
        if chat and chat.available_reactions and chat.available_reactions.reactions:
            emojis = [
                r.emoji
                for r in chat.available_reactions.reactions
                if r.emoji is not None
            ]
            raise ModelRetry(
                f"Invalid reaction emoji. This chat supports the following reactions: {', '.join(emojis)}"
            )
        raise ModelRetry("Invalid reaction emoji, try another one.")
    except Exception as e:
        logger.error(f"send_reaction error: {e.__class__.__name__}: {e}")
        raise ModelRetry(f"Failed to send reaction: {e.__class__.__name__}: {e}")
    # Mark as already called this turn so prepare_periodic_reaction suppresses
    # the "MUST call" hint for any further steps within the same agent run.
    ctx.deps.tools_called_this_turn.add("send_reaction")
    return SendResult(success=True).text()


@dataclass
class Artist:
    name: str
    type: str
    username: str
    uid: str


@dataclass
class AnimePhotoInfo:
    title: str
    source_url: str
    r18: bool
    description: str | None = None
    artist: Artist | None = None
    tags: list[str] | None = None


@dataclass
class AnimePhotoResult:
    success: bool = True
    message: str | None = None
    data: AnimePhotoInfo | None = None


async def _fetch_anime_artwork(keyword: str = "") -> tuple[dict, dict] | None:
    try:
        if keyword:
            params = {
                "r18": 2,
                "hybrid": app_config.manyacg_hybrid_search,
                "keyword": keyword,
            }
            resp = await manyacg.httpx_client.get(
                url="/artwork/list",
                params=params,
            )
        else:
            resp = await manyacg.httpx_client.get(
                url="/artwork/random",
                params={"r18": 2},
            )
        if resp.status_code != 200:
            logger.error(f"Anime API returned {resp.status_code}")
            return None
        artwork: dict = random.choice(resp.json()["data"])
        picture: dict = artwork["pictures"][
            random.randint(0, len(artwork["pictures"]) - 1)
        ]
        return artwork, picture
    except Exception as e:
        logger.error(f"Fetch anime artwork error: {e.__class__.__name__}:{e}")
        return None


async def _send_anime_photo_guest(
    ctx: RunContext[datatype.ContextDeps],
    artwork: dict,
    picture: dict,
) -> AnimePhotoResult:
    query_id = ctx.deps.message.guest_query_id
    if ctx.deps.guest_replied or not query_id:
        return AnimePhotoResult(
            success=False, message="Guest query already replied."
        )
    photo_url = picture["regular"]
    caption = f"{artwork['title']}\n{artwork['source_url']}"

    try:
        result = InputBotInlineResult(
            id="0",
            type="photo",
            title=artwork["title"],
            thumb=InputWebDocument(
                url=photo_url,
                size=0,
                mime_type="image/jpeg",
                attributes=[DocumentAttributeImageSize(w=0, h=0)],
            ),
            content=InputWebDocument(
                url=photo_url,
                size=0,
                mime_type="image/jpeg",
                attributes=[DocumentAttributeImageSize(w=0, h=0)],
            ),
            send_message=InputBotInlineMessageMediaAuto(
                message=caption,
            ),
        )
        await ctx.deps.client.invoke(
            SetBotGuestChatResult(
                query_id=int(query_id),
                result=result,
            )
        )
        ctx.deps.guest_replied = True
    except Exception as e:
        logger.error(f"Guest anime photo send error: {e.__class__.__name__} - {e}")
        return AnimePhotoResult(
            success=False, message=f"Failed to send photo: {e.__class__.__name__}"
        )

    return AnimePhotoResult(
        success=True,
        data=AnimePhotoInfo(
            title=artwork["title"],
            source_url=artwork["source_url"],
            r18=artwork["r18"],
            description=artwork.get("description", "")[:512],
            artist=Artist(
                name=artwork.get("artist", {}).get("name", ""),
                type=artwork["artist"].get("type", ""),
                username=artwork["artist"].get("username", ""),
                uid=artwork["artist"].get("uid", ""),
            ),
            tags=artwork.get("tags", [])[:10],
        ),
    )


async def send_anime_photo(
    ctx: RunContext[datatype.ContextDeps], keyword: str = ""
) -> AnimePhotoResult:
    """Get and send anime photos (or called it setu/涩图).

    Args:
        keyword: Optional keyword to search for specific anime photos.

    Returns:
        An AnimePhotoResult dataclass containing the result of the operation.
    """
    if ctx.deps.message is None or ctx.deps.message.id is None:
        return AnimePhotoResult(
            success=False, message="Current message context is unavailable."
        )
    if (
        ctx.deps.chat_id is not None
        and ctx.deps.chat_id != ctx.deps.user_id
        and not (await database.get_chat_config(ctx.deps.chat_id)).setu_enabled
    ):
        return AnimePhotoResult(
            success=False, message="Anime photo feature is disabled in this chat."
        )
    try:
        ratekey = f"anime_photo_rate_limit:{ctx.deps.chat_id}:{ctx.deps.user_id}"
        if await common.memttlcache.get(ratekey, 0) > 3:
            return AnimePhotoResult(
                success=False,
                message="You are sending requests too frequently. Please try again later.",
            )
        current_count = await common.memttlcache.get(ratekey, 0)
        await common.memttlcache.set(ratekey, current_count + 1, ttl=10)

        fetched = await _fetch_anime_artwork(keyword)
        if fetched is None:
            return AnimePhotoResult(
                success=False, message="Failed to fetch anime artwork."
            )
        artwork, picture = fetched

        if ctx.deps.is_guest_mode:
            return await _send_anime_photo_guest(ctx, artwork, picture)

        user_config = await database.get_user_config(ctx.deps.user_id)
        lang = user_config.lang
        detail_link = (
            f"https://t.me/{app_config.manyacg_channel}/{picture['message_id']}"
            if picture.get("message_id")
            else artwork["source_url"]
        )
        await ctx.deps.client.send_photo(
            chat_id=ctx.deps.chat_id,
            photo=picture["regular"],
            caption=f"<a href='{artwork['source_url']}'>{artwork['title']}</a>",
            parse_mode=pyrogram.enums.ParseMode.HTML,
            reply_markup=pyrogram.types.InlineKeyboardMarkup(
                [
                    [
                        pyrogram.types.InlineKeyboardButton(
                            text=i18n.t("bot.button.manyacg.detail", locale=lang),
                            url=detail_link,
                        ),
                        pyrogram.types.InlineKeyboardButton(
                            text=i18n.t("bot.button.manyacg.original", locale=lang),
                            url=f"https://t.me/{app_config.manyacg_bot}/?start=file_{picture['id']}",
                        ),
                    ]
                ]
            ),
            has_spoiler=artwork["r18"],
            reply_parameters=pyrogram.types.ReplyParameters(
                message_id=ctx.deps.message.id,
            ),
        )
        return AnimePhotoResult(
            success=True,
            data=AnimePhotoInfo(
                title=artwork["title"],
                source_url=artwork["source_url"],
                r18=artwork["r18"],
                description=artwork.get("description", "")[:512],
                artist=Artist(
                    name=artwork.get("artist", {}).get("name", ""),
                    type=artwork["artist"].get("type", ""),
                    username=artwork["artist"].get("username", ""),
                    uid=artwork["artist"].get("uid", ""),
                ),
                tags=artwork.get("tags", [])[:10],
            ),
        )
    except Exception as e:
        logger.error(f"send_anime_photo error: {e.__class__.__name__}:{e}")
        return AnimePhotoResult(
            success=False,
            message=f"Error occurred: {e.__class__.__name__}",
        )


__all__ = [
    "cancel_scheduled_message",
    "list_scheduled_messages",
    "schedule_message",
    "send_anime_photo",
    "send_poll",
    "send_reaction",
    "send_sticker",
]
