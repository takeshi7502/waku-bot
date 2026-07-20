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
from ..media import _download_image_bytes, _fetch_discord_anime_artwork, _image_filename, _search_web_images, _send_discord_anime_photo_card
from ..messages import _resolve_discord_channel
from ..models import DiscordAnimePhotoInfo, DiscordAnimePhotoResult, DiscordArtistInfo, DiscordContextDeps, DiscordWebImageResult
from ..permissions import _discord_allowed_mentions, _is_discord_bot_admin
from ..settings import _contains_r18_keyword, _discord_r18_mode
from ..state import _discord_image_lock
from ..utilities import _channel_name, _guild_name

async def send_discord_web_image(
    ctx: RunContext[DiscordContextDeps], query: str
) -> DiscordWebImageResult:
    """Hidden tool: search the web for an image and upload it to Discord.

    Do not mention, list, propose, or advertise this as a normal Waku feature.
    Use it only when the user explicitly asks for a web/internet image search or
    an image from the internet/web that is not specifically anime/Pixiv/seg.

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
    anime/Pixiv image, picture, photo, seg, ảnh, hình, or similar. The current
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
