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
from .settings import _discord_guild_settings, _discord_r18_mode, _r18_mode_label
from .state import _discord_image_lock
from .utilities import _channel_name, _guild_name

def _find_artwork_url(content: str) -> str | None:
    for regex in manyacg_service.ARTWORK_ALL_REGEX:
        match = regex.search(content)
        if match:
            artwork_url = match.group()
            if not artwork_url.startswith("http"):
                artwork_url = "https://" + artwork_url
            return artwork_url
    return None

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

async def _send_discord_setu(message: discord.Message) -> bool:
    if manyacg_client is None:
        await message.channel.send("ManyACG is not configured, so Waku cannot send images yet.", reference=message)
        return True

    if message.guild is not None:
        settings = await _discord_guild_settings(message.guild)
        if not settings.setu_enabled:
            await message.channel.send(
                i18n.t("bot.msg.manyacg.chat_setu_disabled", locale=settings.lang),
                reference=message,
            )
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
