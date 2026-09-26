"""Minimal text chat for Telegram Business connections."""

import asyncio
import weakref

from pydantic_ai import Agent
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import BusinessConnection, Message

from waku.common.memory_store import memttlcache
from waku.config import app_config
from waku.logger import logger


_chat_locks: weakref.WeakValueDictionary[tuple[str, int], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_connection_permissions: dict[str, tuple[bool, int | None]] = {}
_HISTORY_LIMIT = 20
_MESSAGE_LIMIT = 4096


def _should_reply(message: Message) -> bool:
    sender = message.from_user
    return bool(
        app_config.business_chat_enabled
        and app_config.agent
        and app_config.agent_model
        and message.business_connection_id
        and message.chat
        and message.chat.id
        and message.text
        and not message.text.startswith("/")
        and not message.outgoing
        and sender
        and not sender.is_bot
    )


def _make_business_agent() -> Agent:
    from waku.plugins.agent.provider import make_chat_model

    assert app_config.agent_model is not None
    return Agent(
        model=make_chat_model(app_config.agent_model),
        instructions=app_config.agent_prompt,
        output_type=str,
    )


def _remember_connection(connection: BusinessConnection) -> tuple[bool, int | None]:
    permission = (
        bool(connection.is_enabled and connection.rights and connection.rights.can_reply),
        connection.user.id if connection.user else None,
    )
    _connection_permissions[connection.id] = permission
    return permission


@Client.on_business_connection(group=0)
async def business_connection_changed(
    client: Client, connection: BusinessConnection
) -> None:
    _remember_connection(connection)


async def _reply_to_business_message(client: Client, message: Message) -> None:
    if not _should_reply(message):
        return
    assert message.chat is not None
    assert message.business_connection_id is not None
    assert message.text is not None

    connection_id = message.business_connection_id
    chat_id = message.chat.id
    try:
        can_reply, owner_id = _connection_permissions.get(connection_id) or _remember_connection(
            await client.get_business_connection(connection_id)
        )
    except Exception:
        logger.exception(f"Could not check Telegram Business connection {connection_id}")
        return
    if not can_reply or (message.from_user and message.from_user.id == owner_id):
        return

    lock_key = (connection_id, chat_id)
    lock = _chat_locks.get(lock_key)
    if lock is None:
        lock = _chat_locks[lock_key] = asyncio.Lock()

    async with lock:
        if not _should_reply(message):
            return
        history_key = f"business_chat_history:{connection_id}:{chat_id}"
        history = await memttlcache.get(history_key, [])
        timeout = app_config.agent_run_timeout if app_config.agent_run_timeout > 0 else 180
        try:
            chat_agent = _make_business_agent()
            result = await asyncio.wait_for(
                chat_agent.run(message.text, message_history=history),
                timeout=timeout,
            )
            answer = result.output.strip()
            if not answer or not app_config.business_chat_enabled:
                return
            # Business sends must carry the connection ID so they come from the
            # connected account, not from the bot's own private chat.
            for start in range(0, len(answer), _MESSAGE_LIMIT):
                if not app_config.business_chat_enabled:
                    return
                await client.send_message(
                    chat_id=chat_id,
                    text=answer[start : start + _MESSAGE_LIMIT],
                    business_connection_id=connection_id,
                    parse_mode=ParseMode.DISABLED,
                )
            await memttlcache.set(
                history_key,
                result.all_messages()[-_HISTORY_LIMIT:],
                ttl=app_config.cachettl_agent_history,
            )
        except TimeoutError:
            logger.warning(
                f"Telegram Business chat timed out for connection {connection_id}, chat {chat_id}"
            )
        except Exception:
            logger.exception(
                f"Telegram Business chat failed for connection {connection_id}, chat {chat_id}"
            )


@Client.on_business_message(filters.private & filters.text, group=0)
async def business_chat_message(client: Client, message: Message) -> None:
    await _reply_to_business_message(client, message)
