import re

import pyrogram
from pyrogram.client import Client as PyrogramClient

from waku.logger import logger
from waku.plugins.agent.styling import convert_md

_RICH_BLOCK_RE = re.compile(
    r"(?m)^(?:#{1,6}\s+|(?:[-+*]|\d+[.)])\s+|\|.+\|\s*$|<details(?:\s|>)|<tg-(?:math-block|collage|slideshow)\b)"
)


def should_use_rich_message(markdown: str) -> bool:
    """Return True only for structures unavailable in normal MessageEntity output."""
    return bool(_RICH_BLOCK_RE.search(markdown))


def _reply_parameters(message: pyrogram.types.Message) -> pyrogram.types.ReplyParameters:
    return pyrogram.types.ReplyParameters(message_id=message.id)


def _message_thread_id(message: pyrogram.types.Message) -> int | None:
    return getattr(message, "message_thread_id", None)


async def send_rich_reply(
    client: PyrogramClient,
    message: pyrogram.types.Message,
    markdown: str,
) -> pyrogram.types.Message:
    """Use Rich Message only when the content contains rich-only structures."""
    chat = message.chat
    if chat is None:
        raise ValueError("Cannot reply without a chat")
    plain, entities = convert_md(markdown)
    if not should_use_rich_message(markdown):
        try:
            return await message.reply_text(plain, entities=entities)
        except Exception as entity_error:
            logger.warning(
                "Entity send failed; sending plain text: "
                f"{entity_error.__class__.__name__} - {entity_error}"
            )
            return await message.reply_text(plain)

    try:
        return await client.send_rich_message(
            chat_id=chat.id,
            rich_message=pyrogram.types.InputRichMessage(markdown=markdown),
            reply_parameters=_reply_parameters(message),
            message_thread_id=_message_thread_id(message),
            business_connection_id=getattr(message, "business_connection_id", None),
        )
    except Exception as rich_error:
        logger.warning(
            "Rich Message send failed; falling back to MessageEntity output: "
            f"{rich_error.__class__.__name__} - {rich_error}"
        )
        try:
            return await message.reply_text(plain, entities=entities)
        except Exception as entity_error:
            logger.warning(
                "Entity fallback failed; sending plain text: "
                f"{entity_error.__class__.__name__} - {entity_error}"
            )
            return await message.reply_text(plain)


async def edit_as_rich_message(
    client: PyrogramClient,
    message: pyrogram.types.Message,
    markdown: str,
) -> pyrogram.types.Message:
    """Use Rich Message edits only when the content needs rich-only structures."""
    chat = message.chat
    if chat is None:
        raise ValueError("Cannot edit without a chat")
    plain, entities = convert_md(markdown)
    if not should_use_rich_message(markdown):
        try:
            return await client.edit_message_text(
                chat_id=chat.id,
                message_id=message.id,
                text=plain,
                entities=entities,
                business_connection_id=getattr(message, "business_connection_id", None),
            )
        except pyrogram.errors.exceptions.bad_request_400.MessageNotModified:
            return message
        except Exception as entity_error:
            logger.warning(
                "Entity edit failed; editing as plain text: "
                f"{entity_error.__class__.__name__} - {entity_error}"
            )
            return await client.edit_message_text(
                chat_id=chat.id,
                message_id=message.id,
                text=plain,
                parse_mode=pyrogram.enums.ParseMode.DISABLED,
                business_connection_id=getattr(message, "business_connection_id", None),
            )

    try:
        return await client.edit_message_text(
            chat_id=chat.id,
            message_id=message.id,
            rich_message=pyrogram.types.InputRichMessage(markdown=markdown),
            business_connection_id=getattr(message, "business_connection_id", None),
        )
    except pyrogram.errors.exceptions.bad_request_400.MessageNotModified:
        return message
    except Exception as rich_error:
        logger.warning(
            "Rich Message edit failed; falling back to MessageEntity output: "
            f"{rich_error.__class__.__name__} - {rich_error}"
        )
        try:
            return await client.edit_message_text(
                chat_id=chat.id,
                message_id=message.id,
                text=plain,
                entities=entities,
                business_connection_id=getattr(message, "business_connection_id", None),
            )
        except pyrogram.errors.exceptions.bad_request_400.MessageNotModified:
            return message
        except Exception as entity_error:
            logger.warning(
                "Entity edit fallback failed; editing as plain text: "
                f"{entity_error.__class__.__name__} - {entity_error}"
            )
            return await client.edit_message_text(
                chat_id=chat.id,
                message_id=message.id,
                text=plain,
                parse_mode=pyrogram.enums.ParseMode.DISABLED,
                business_connection_id=getattr(message, "business_connection_id", None),
            )
