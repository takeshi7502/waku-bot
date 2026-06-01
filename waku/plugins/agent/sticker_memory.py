import asyncio
import json
import re
from io import BytesIO

import pyrogram
import pyrogram.types
from pydantic_ai import Agent, BinaryContent, Embedder
from pydantic_ai.embeddings import EmbeddingSettings
from pyrogram import filters
from pyrogram.client import Client as PyrogramClient

from waku import common, database
from waku.config import app_config
from waku.logger import logger

from . import provider, sticker_vec
from .whitelist import is_chat_allowed

embedder: Embedder | None = None
_description_agent: Agent[None, str] | None = None
_embedding_agent: Agent[None, str] | None = None

if app_config.agent_sticker_memory:
    # Sticker descriptions need vision/multimodal capability when possible.
    # This is independent from the main chat model so the chat model can be
    # swapped without breaking sticker understanding.
    _desc_spec = (
        app_config.agent_sticker_description_model
        or app_config.agent_model_multimodal
        or app_config.agent_model
    )
    # Prefer a real embeddings model via agent_sticker_embed_model. If it is
    # missing, use the main chat model to generate JSON vectors. This avoids
    # requiring the vision model to also behave like an embedding model.
    _embed_spec = app_config.agent_sticker_embed_model
    if _embed_spec is None:
        chat_embed_spec = app_config.agent_model
        assert chat_embed_spec is not None
        _embedding_agent = Agent(
            model=provider.make_chat_model(chat_embed_spec),
            output_type=str,
            retries=2,
        )
    elif _embed_spec.startswith("chat/"):
        chat_embed_spec = _embed_spec.removeprefix("chat/")
        _embedding_agent = Agent(
            model=provider.make_chat_model(chat_embed_spec),
            output_type=str,
            retries=2,
        )
    else:
        embedder = Embedder(
            provider.make_embed_model(_embed_spec),
            settings=EmbeddingSettings(
                dimensions=app_config.agent_sticker_embed_dimensions
            ),
        )

    _description_agent = Agent(
        model=provider.make_chat_model(_desc_spec),
        output_type=str,
        retries=2,
    )


def _parse_embedding_vector(text: str, dimensions: int) -> list[float] | None:
    """Parse a JSON embedding vector returned by a chat model."""
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return None
    try:
        values = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(values, list):
        return None
    vector: list[float] = []
    for value in values[:dimensions]:
        if not isinstance(value, int | float):
            return None
        vector.append(float(value))
    if len(vector) != dimensions:
        return None
    return vector


async def _get_chat_embedding(text: str) -> list[float] | None:
    if _embedding_agent is None:
        return None
    dimensions = app_config.agent_sticker_embed_dimensions
    prompt = (
        "You are emulating an embedding model for sticker search because no "
        "native embeddings model is configured. "
        f"Convert this sticker search text into a semantic embedding vector of exactly {dimensions} numbers. "
        "Return ONLY a JSON array. Each number must be between -1 and 1. "
        "Similar moods/emotions/meanings should produce similar vectors. Text: "
        f"{text!r}"
    )
    try:
        result = await _embedding_agent.run(prompt)
        return _parse_embedding_vector(result.output, dimensions)
    except Exception as e:
        logger.error(f"sticker chat embed error: {e.__class__.__name__}: {e}")
        return None


async def get_embedding(text: str) -> list[float] | None:
    if _embedding_agent is not None:
        return await _get_chat_embedding(text)
    if embedder is None:
        return None
    try:
        result = await embedder.embed_query(text)
        return list(result.embeddings[0])
    except Exception as e:
        logger.error(f"sticker embed error: {e.__class__.__name__}: {e}")
        return None


async def _get_description(image_bytes: bytes, mime_type: str) -> str | None:
    if _description_agent is None:
        return None
    try:
        if mime_type == "video/webm":
            frame = await common.webm_first_frame(image_bytes)
            if frame is None:
                return None
            content_part: BinaryContent = BinaryContent(
                data=frame,
                media_type="image/webp",  # type: ignore
            )
        else:
            content_part = BinaryContent(data=image_bytes, media_type=mime_type)  # type: ignore

        # 使用超时控制防止模型调用阻塞事件循环（贴纸描述使用小模型超时）
        timeout = app_config.agent_small_model_timeout
        coro = _description_agent.run(
            [content_part, app_config.agent_sticker_description_prompt]
        )

        if timeout > 0:
            try:
                result = await asyncio.wait_for(coro, timeout=timeout)
            except TimeoutError:
                logger.warning(f"sticker description timed out after {timeout}s")
                return None
        else:
            result = await coro

        return result.output
    except Exception as e:
        logger.error(f"sticker description error: {e.__class__.__name__}: {e}")
        return None


async def _process_sticker(
    client: PyrogramClient,
    sticker: pyrogram.types.Sticker,
    chat_id: int,
) -> None:
    file_unique_id = sticker.file_unique_id
    file_id = sticker.file_id

    if await sticker_vec.exists(file_unique_id, chat_id):
        await sticker_vec.touch(file_unique_id, chat_id)
        logger.debug(
            f"sticker memory touched existing: chat_id={chat_id} sticker={file_unique_id}"
        )
        return

    if sticker.is_animated:
        logger.info(
            f"sticker memory skipped animated: chat_id={chat_id} sticker={file_unique_id}"
        )
        return

    if sticker.is_video and common.FFMPEG is None:
        logger.warning(
            f"sticker memory skipped video without ffmpeg: chat_id={chat_id} "
            f"sticker={file_unique_id}"
        )
        return

    try:
        # 使用超时控制防止下载大文件阻塞事件循环
        timeout = app_config.agent_download_timeout
        if timeout > 0:
            raw = await asyncio.wait_for(
                client.download_media(file_id, in_memory=True), timeout=timeout
            )
        else:
            raw = await client.download_media(file_id, in_memory=True)
        if not isinstance(raw, BytesIO):
            logger.warning(
                f"sticker memory download returned non-bytes: chat_id={chat_id} "
                f"sticker={file_unique_id} type={type(raw).__name__}"
            )
            return
        image_bytes = raw.getvalue()
    except TimeoutError:
        logger.warning(f"sticker download timed out for {file_unique_id}")
        return
    except Exception as e:
        logger.error(f"sticker download error: {e.__class__.__name__}: {e}")
        return

    mime_type = "video/webm" if sticker.is_video else "image/webp"
    description = await _get_description(image_bytes, mime_type)
    if not description:
        logger.warning(f"sticker {file_unique_id}: no description generated, skipping")
        return

    embedding = await get_embedding(description)
    if not embedding:
        logger.warning(f"sticker {file_unique_id}: no embedding generated, skipping")
        return

    await sticker_vec.upsert(file_unique_id, file_id, chat_id, description, embedding)
    sticker_count = await sticker_vec.count(chat_id)
    logger.info(
        f"sticker memory saved ({sticker_count}/10): chat_id={chat_id} "
        f"sticker={file_unique_id} description={description[:80]!r}"
    )


_sticker_filter = filters.sticker & (filters.group) & ~filters.bot


@PyrogramClient.on_message(_sticker_filter, group=11)
async def on_sticker(client: PyrogramClient, message: pyrogram.types.Message) -> None:
    if not app_config.agent:
        return
    if not app_config.agent_sticker_memory:
        return
    if _description_agent is None or (embedder is None and _embedding_agent is None):
        return
    chat = message.chat
    if not chat or not chat.id:
        return
    if not is_chat_allowed(chat.id):
        logger.info(f"sticker memory ignored disabled chat: chat_id={chat.id}")
        return
    sticker = message.sticker
    if sticker is None:
        return
    if not common.random_chance(app_config.agent_sticker_memory_sample_rate):
        logger.info(
            f"sticker memory sampled out: chat_id={chat.id} "
            f"rate={app_config.agent_sticker_memory_sample_rate}"
        )
        return
    chat_config = await database.get_chat_config(chat.id)
    if not chat_config.ai_reply:
        logger.info(f"sticker memory ignored because ai_reply is off: chat_id={chat.id}")
        return
    logger.info(
        f"sticker memory processing: chat_id={chat.id} "
        f"sticker={sticker.file_unique_id} video={bool(sticker.is_video)}"
    )
    asyncio.create_task(_process_sticker(client, sticker, chat.id))
