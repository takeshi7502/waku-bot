import asyncio

import pyrogram
from pyrogram.errors import RPCError

from waku import common, database, i18n


async def _can_clean(client: pyrogram.Client, message: pyrogram.types.Message) -> bool:
    user = message.sender_chat or message.from_user
    chat = message.chat
    if not user or not chat:
        return False
    return await common.can_user_manage_bot_in_chat(user, chat, 'delete messages')


async def _delete_ids(client: pyrogram.Client, chat_id: int, ids: list[int]) -> int:
    deleted = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        try:
            await client.delete_messages(chat_id, chunk)
            deleted += len(chunk)
        except RPCError:
            for msg_id in chunk:
                try:
                    await client.delete_messages(chat_id, msg_id)
                    deleted += 1
                except RPCError:
                    pass
    return deleted


async def _clean_after(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    *,
    bot_only: bool,
) -> None:
    chat = message.chat
    if not chat or chat.id is None:
        return
    chat_config = await database.get_chat_config(chat.id)
    if not await _can_clean(client, message):
        await message.reply_text(i18n.t('bot.msg.no_permission_group', locale=chat_config.lang))
        return
    if not message.reply_to_message:
        cmd = '/clean' if bot_only else '/cleanall'
        await message.reply_text(f'Hãy reply vào mốc tin nhắn rồi dùng {cmd}.')
        return

    marker_id = message.reply_to_message.id
    end_id = message.id
    if end_id <= marker_id:
        await message.reply_text('Không có tin nhắn nào sau mốc reply để xoá.')
        return

    me = await client.get_me()
    ids: list[int] = []
    for msg_id in range(marker_id + 1, end_id + 1):
        try:
            msg = await client.get_messages(chat.id, msg_id)
        except RPCError:
            continue
        if not msg or getattr(msg, 'empty', False):
            continue
        if bot_only:
            sender = msg.from_user or msg.sender_chat
            if not sender or sender.id != me.id:
                continue
        ids.append(msg_id)
        if len(ids) >= 1000:
            break

    if message.id not in ids:
        ids.append(message.id)
    deleted = await _delete_ids(client, chat.id, ids)
    status = await message.reply_text(f'Đã xoá {deleted} tin nhắn.', quote=False)

    async def _delete_status_later():
        await asyncio.sleep(5)
        try:
            await status.delete()
        except Exception:
            pass

    asyncio.create_task(_delete_status_later())


@pyrogram.Client.on_message(pyrogram.filters.command('clean') & pyrogram.filters.group, group=0)
async def clean_bot_messages(client: pyrogram.Client, message: pyrogram.types.Message):
    '''Reply to a marker message, then delete bot messages after it.'''
    await _clean_after(client, message, bot_only=True)


@pyrogram.Client.on_message(pyrogram.filters.command('cleanall') & pyrogram.filters.group, group=0)
async def clean_all_messages(client: pyrogram.Client, message: pyrogram.types.Message):
    '''Reply to a marker message, then delete all messages after it.'''
    await _clean_after(client, message, bot_only=False)
