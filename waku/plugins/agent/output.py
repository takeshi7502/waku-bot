import asyncio
import random
import re
from datetime import datetime

import pyrogram
import pyrogram.errors
from pyrogram.client import Client as PyrogramClient

from waku.common.memory_store import memttlcache
from waku.logger import logger
from waku.plugins.agent import datatype, state
from waku.plugins.agent.guest_mode import answer_guest_query
from waku.plugins.agent.message_text import get_message_text_markdown
from waku.plugins.agent.rich_output import edit_as_rich_message, send_rich_reply
from waku.plugins.agent.styling import convert_md

_MD_SEPARATOR_RE = re.compile(r"^(?:[-*_][ \t]*){3,}$")

TELEGRAM_SAFE_MESSAGE_LENGTH = 4096
TELEGRAM_RICH_MESSAGE_LENGTH = 8192


def _split_text_for_telegram(text: str, limit: int = TELEGRAM_SAFE_MESSAGE_LENGTH) -> list[str]:
    """Split text into Telegram-safe chunks without silently truncating output."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks



def _is_markdown_separator_only_chunk(chunk: str) -> bool:
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    return bool(lines) and all(_MD_SEPARATOR_RE.fullmatch(line) for line in lines)


async def reply_output(
    client: PyrogramClient,
    message: pyrogram.types.Message,
    text: str,
    deps: "datatype.ContextDeps | None" = None,
):
    if message.guest_query_id:
        return await answer_guest_query(client, message, text, deps=deps)
    if message.chat is None:
        return
    is_group_chat = message.chat.type in (
        pyrogram.enums.ChatType.SUPERGROUP,
        pyrogram.enums.ChatType.GROUP,
    )
    user = message.sender_chat or message.from_user
    if len(text) > TELEGRAM_SAFE_MESSAGE_LENGTH:
        safe_text_chunks = _split_text_for_telegram(text, TELEGRAM_RICH_MESSAGE_LENGTH)
    else:
        safe_text_chunks = _split_text_for_telegram(text)
    lines = [line for line in text.split("\n\n") if line.strip()]
    if not lines:
        return
    total_plain, total_entities = convert_md(text)
    has_block = False
    for e in total_entities:
        if (
            e.type == pyrogram.enums.MessageEntityType.BLOCKQUOTE
            or e.type == pyrogram.enums.MessageEntityType.PRE
        ):
            has_block = True
            break

    max_messages = 7
    total_sentences = len(lines)
    num_messages = min(max_messages, total_sentences)

    base = total_sentences // num_messages
    remainder = total_sentences % num_messages

    chunks: list[str] = []
    index = 0
    for i in range(num_messages):
        size = base + (1 if i < remainder else 0)
        part = lines[index : index + size]
        index += size
        chunks.append("\n".join(part))
    try:
        last_reply_msg: pyrogram.types.Message | None = None
        if has_block or len(text) > TELEGRAM_SAFE_MESSAGE_LENGTH:
            for raw_chunk in safe_text_chunks:
                last_reply_msg = await send_rich_reply(
                    client, message, raw_chunk
                )
                await asyncio.sleep(random.uniform(0.3, 1.2) + len(raw_chunk) / 1000)
        else:
            chunks = [part for chunk in chunks for part in _split_text_for_telegram(chunk)]
            for chunk in chunks:
                # 如果只有分隔符, 则跳过
                if _is_markdown_separator_only_chunk(chunk):
                    continue
                await message.reply_chat_action(pyrogram.enums.ChatAction.TYPING)
                reply_msg = await send_rich_reply(client, message, chunk)
                last_reply_msg = reply_msg
                await asyncio.sleep(random.uniform(0.721, 3.9) + len(chunk) / 600)
        if (
            last_reply_msg
            and is_group_chat
            and user
            and user.id
        ):
            bot_reply = datatype.BotLastReply(
                message_id=last_reply_msg.id,
                reply_to_user_id=user.id,
                reply_to_message_id=message.id,
                reply_text=get_message_text_markdown(last_reply_msg) or text,
                original_user_message=get_message_text_markdown(message),
                timestamp=datetime.now().timestamp(),
            )
            _chat = message.chat
            _chat_id = _chat.id if _chat else None
            if _chat_id:
                await memttlcache.set(
                    state.bot_last_reply_key(_chat_id),
                    bot_reply,
                    ttl=300,
                )
    except Exception as e:
        logger.error(f"Error replying message: {e.__class__.__name__} - {e}")


class TypingKeepAlive:
    """Maintains a typing chat action for the duration of a long-running operation.

    This is a standalone context manager that keeps sending TYPING status
    independently of StreamingOutput, so typing continues during tool calls too.
    """

    CHAT_ACTION_INTERVAL = 4

    def __init__(self, client: PyrogramClient, message: pyrogram.types.Message):
        self.client = client
        self.message = message
        self._stop = False
        self._task: asyncio.Task | None = None

    async def _loop(self):
        chat = self.message.chat
        chat_id = chat.id if chat else None
        if not chat_id:
            return
        first = True
        while not self._stop:
            try:
                if not first:
                    await asyncio.sleep(self.CHAT_ACTION_INTERVAL)
                    if self._stop:
                        break
                first = False
                await self.client.send_chat_action(
                    chat_id=chat_id,
                    action=pyrogram.enums.ChatAction.TYPING,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"TypingKeepAlive: error sending chat action: {e}")
                break

    def start(self):
        self._stop = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def __aenter__(self):
        self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()


class StreamingOutput:
    STREAM_EDIT_INTERVAL = 1.5
    # Streaming previews still use the Bot API text limit to avoid edit errors.
    # The final response may exceed 4096 and is sent/edited as Rich Message.
    MAX_PREVIEW_LENGTH = TELEGRAM_SAFE_MESSAGE_LENGTH
    MAX_FINAL_MESSAGE_LENGTH = TELEGRAM_RICH_MESSAGE_LENGTH
    MAX_EDIT_COUNT = 20
    MAX_TOTAL_TIME = 120.0

    def __init__(
        self,
        client: PyrogramClient,
        message: pyrogram.types.Message,
        deps: "datatype.ContextDeps | None" = None,
    ):
        self.client = client
        self.message = message
        self.deps = deps
        self.current_text = ""
        self._last_sent_text = ""
        self.reply_message: pyrogram.types.Message | None = None
        self.last_edit_time = 0.0
        self.edit_count = 0
        self.start_time = 0.0
        self.is_group_chat = message.chat and message.chat.type in (
            pyrogram.enums.ChatType.SUPERGROUP,
            pyrogram.enums.ChatType.GROUP,
        )
        self.user = message.sender_chat or message.from_user
        self._edit_task: asyncio.Task | None = None
        self._start_task: asyncio.Task | None = None
        self._stop = False
        self.is_guest = bool(message.guest_query_id)

    def _is_within_limits(self) -> bool:
        current_time = asyncio.get_event_loop().time()
        if self.start_time == 0.0:
            self.start_time = current_time
        elapsed = current_time - self.start_time
        if elapsed > self.MAX_TOTAL_TIME:
            logger.warning(f"Streaming output exceeded max time {self.MAX_TOTAL_TIME}s")
            return False
        if self.edit_count >= self.MAX_EDIT_COUNT:
            logger.warning(
                f"Streaming output exceeded max edit count {self.MAX_EDIT_COUNT}"
            )
            return False
        return True

    async def _do_edit(self, text: str):
        if not self.reply_message:
            return
        try:
            # During streaming, send plain text without entities to avoid
            # rendering partially-formed markdown. Entities applied at finalize.
            await self.reply_message.edit_text(
                text[: self.MAX_PREVIEW_LENGTH],
                parse_mode=pyrogram.enums.ParseMode.DISABLED,
            )
            self._last_sent_text = text
            self.last_edit_time = asyncio.get_event_loop().time()
            self.edit_count += 1
        except pyrogram.errors.exceptions.bad_request_400.MessageNotModified:
            self._last_sent_text = text
        except pyrogram.errors.exceptions.bad_request_400.MessageTooLong:
            await self._send_new_message(text)
        except Exception as e:
            logger.error(f"Error editing message: {e.__class__.__name__} - {e}")

    async def _send_new_message(self, text: str) -> bool:
        plain, entities = convert_md(text)
        if not plain.strip():
            return False
        try:
            self.reply_message = await self.message.reply_text(
                plain[: self.MAX_PREVIEW_LENGTH],
                entities=entities if len(plain) <= self.MAX_PREVIEW_LENGTH else None,
            )
        except Exception as e:
            logger.error(f"Send failed in streaming: {e}")
            raise
        self._last_sent_text = text
        self.last_edit_time = asyncio.get_event_loop().time()
        self.edit_count += 1
        return True

    async def _edit_loop(self):
        while not self._stop:
            await asyncio.sleep(self.STREAM_EDIT_INTERVAL)
            if self._stop:
                break
            if not self._is_within_limits():
                break
            text = self.current_text
            if not text.strip() or text == self._last_sent_text:
                continue
            await self._do_edit(text)

    async def _start(self):
        if self.is_guest:
            return
        if not await self._send_new_message(self.current_text):
            return
        self._edit_task = asyncio.create_task(self._edit_loop())

    async def append_delta(self, delta: str):
        if not delta:
            return
        self.current_text += delta
        if self.is_guest:
            return
        if self.start_time == 0.0 and self.current_text.strip():
            self.start_time = asyncio.get_event_loop().time()
            self._stop = False
            self._start_task = asyncio.create_task(self._start())

    async def finalize(self):
        self._stop = True
        if self.is_guest:
            if self.current_text:
                from waku.plugins.agent.guest_mode import answer_guest_query

                await answer_guest_query(self.client, self.message, self.current_text, deps=self.deps)
            return
        if self._start_task and not self._start_task.done():
            await self._start_task
        if self._edit_task and not self._edit_task.done():
            self._edit_task.cancel()
            try:
                await self._edit_task
            except asyncio.CancelledError:
                pass
        if self.reply_message and self.current_text:
            text = self.current_text
            plain, entities = convert_md(text)
            if not plain.strip():
                return
            if text != self._last_sent_text or entities:
                try:
                    final_chunks = _split_text_for_telegram(
                        text, self.MAX_FINAL_MESSAGE_LENGTH
                    )
                    self.reply_message = await edit_as_rich_message(
                        self.client,
                        self.reply_message,
                        final_chunks[0],
                    )
                    for extra_chunk in final_chunks[1:]:
                        self.reply_message = await send_rich_reply(
                            self.client, self.message, extra_chunk
                        )
                    self._last_sent_text = text
                except Exception as e:
                    logger.error(f"Error editing final message: {e}")
            elif not self.reply_message:
                await self._send_new_message(text)
        if self.reply_message and self.is_group_chat and self.user and self.user.id:
            bot_reply = datatype.BotLastReply(
                message_id=self.reply_message.id,
                reply_to_user_id=self.user.id,
                reply_to_message_id=self.message.id,
                reply_text=self.current_text,
                original_user_message=get_message_text_markdown(self.message),
                timestamp=datetime.now().timestamp(),
            )
            chat = self.message.chat
            chat_id = chat.id if chat else None
            if chat_id:
                await memttlcache.set(
                    state.bot_last_reply_key(chat_id),
                    bot_reply,
                    ttl=300,
                )

    async def abort(self):
        self._stop = True
        for task in (self._start_task, self._edit_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
