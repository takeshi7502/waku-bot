import asyncio

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import ChatMemberUpdated, Message

from waku import common, database
from waku.config import app_config
from waku.i18n import i18n
from waku.logger import logger
from waku.plugins.member_ops import (
    acquire_group_member_operation,
    describe_group_member_operation,
    get_group_member_operation,
    release_group_member_operation,
)

SYNC_MEMBERS_PROGRESS_INTERVAL = 25
SYNC_MEMBERS_SAFE_DELAY_SECONDS = 0.15
SYNC_MEMBERS_PROGRESS_BAR_WIDTH = 12


def _sync_members_progress_bar(scanned: int, total: int | None) -> str:
    if not total or total <= 0:
        return ""
    filled = min(SYNC_MEMBERS_PROGRESS_BAR_WIDTH, round(scanned / total * SYNC_MEMBERS_PROGRESS_BAR_WIDTH))
    empty = SYNC_MEMBERS_PROGRESS_BAR_WIDTH - filled
    return "█" * filled + "░" * empty


def _format_sync_members_progress(
    lang: str,
    scanned: int,
    updated: int,
    total: int | None,
    skipped_existing: int = 0,
    partial: bool = False,
) -> str:
    if total and total > 0:
        text = i18n.t("bot.msg.sync_members_progress", locale=lang).format(
            bar=_sync_members_progress_bar(scanned, total),
            scanned=scanned,
            total=total,
            updated=updated,
        )
    else:
        text = i18n.t("bot.msg.sync_members_progress_unknown_total", locale=lang).format(
            scanned=scanned,
            updated=updated,
        )
    if skipped_existing:
        text += f"\nĐã bỏ qua không đổi: {skipped_existing}"
    if partial:
        text += "\n⚠️ Đồng bộ chưa đủ tổng member, sẽ không xoá member vắng mặt trong lần scan này."
    return text


def _member_snapshot_tuple(member) -> tuple[str | None, str | None, bool]:
    status = getattr(member, "status", None)
    status_value = getattr(status, "value", str(status)) if status is not None else None
    tag = getattr(member, "custom_title", None) or getattr(member, "title", None)
    is_admin = status_value in {"owner", "administrator"}
    return status_value, tag, is_admin


async def _safe_edit_sync_status(status_message: Message, text: str) -> None:
    try:
        await status_message.edit_text(text)
    except Exception as e:
        logger.debug(f"Failed to edit syncmembers status message: {e}")


@Client.on_chat_member_updated(filters.group, group=0)
async def chat_member_updated(client: Client, chat_member_updated: ChatMemberUpdated):
    """
    seems only group admin can receive this event
    """
    chat = chat_member_updated.chat
    old_obj = chat_member_updated.old_chat_member
    new_obj = chat_member_updated.new_chat_member
    """
    1. old_obj is None, new_obj.user is not None:
         This means a new user has joined the chat.
    2. old_obj is not None, new_obj is None:
        This means a user has left the chat.
    3. both old_obj and new_obj are not None, but new_obj.status == BANNED:
        This means a user has been banned from the chat.
    ---
    other cases we don't care about:
    4. new_obj is None, old_obj.status == BANNED:
        This means a user has been unbanned from the chat, but the user is not in the chat.
    5. old_obj is not None, new_obj is not None, new_obj.status != BANNED:
        This means a user has changed their status in the chat (e.g., from member to admin).
    """
    if not any((old_obj, new_obj)):
        return
    user = new_obj.user if new_obj else old_obj.user
    if user is None:
        return
    if user.is_deleted:
        logger.warning(f"User is deleted: {user.id}")
        return
    if user.full_name is None:
        logger.warning(f"user.full_name is None: {user}")
        return
    db_user = await database.upsert_user(user)
    db_chat = await database.upsert_chat(chat)
    if not db_user or not db_chat:
        return
    if new_obj is not None:
        await database.upsert_member_snapshot(db_chat, new_obj)
    # if old_obj is None and new_obj is not None:
    #     # Joined the chat
    #     logger.info(f"[{chat.id}]({user.id}): {user.full_name} joined the chat")
    #     await database.add_association_in_chat(db_chat, db_user)
    if (old_obj is not None and old_obj.user and new_obj is None) or (
        old_obj is not None
        and new_obj is not None
        and new_obj.status == ChatMemberStatus.BANNED
    ):
        logger.info(f"[{chat.id}]({user.id}): {user.full_name} left the chat")
        await database.remove_association(db_user.id, db_chat.id)


@Client.on_message(filters.group & filters.left_chat_member, group=0)
async def on_left_chat_member(client: Client, message: Message):
    """
    Handle the event when a user leaves a group chat.
    This is a fallback for cases where the chat_member_updated event does not trigger.
    """
    chat = message.chat
    user = message.left_chat_member
    if not user or not chat:
        return
    db_user = await database.get_user_by_id(user.id)
    db_chat = await database.upsert_chat(chat)
    if not db_user or not db_chat:
        return
    logger.info(f"[{chat.id}]({user.id}): {user.full_name} left the chat")
    await database.remove_association(db_user.id, db_chat.id)


@Client.on_message(filters.group & filters.command("syncmembers"), group=0)
async def sync_chat_members(client: Client, message: Message):
    user = message.sender_chat or message.from_user
    chat = message.chat
    if not user or not chat or not chat.id:
        return
    db_chat = await database.upsert_chat(chat)
    if not db_chat:
        logger.error(f"Failed to upsert chat {chat.id}")
        return
    lang = db_chat.chat_config.lang
    if not await common.can_user_manage_bot_in_chat(user, chat):
        await message.reply_text(i18n.t("bot.msg.no_permission_group", locale=lang))
        return
    running_op = get_group_member_operation(chat.id)
    if running_op is not None:
        await message.reply_text(f"Đang chạy {describe_group_member_operation(running_op)} cho group này, vui lòng chờ xong rồi thử lại.")
        return
    if await common.memttlcache.get(f"sync_members:{chat.id}"):
        await message.reply_text(i18n.t("bot.msg.sync_members_cd", locale=lang))
        return
    operation = acquire_group_member_operation(chat.id, "syncmembers", getattr(user, "id", None))
    if operation is None:
        await message.reply_text("Đang có tác vụ member khác chạy trong group này.")
        return
    await common.memttlcache.set(
        f"sync_members:{chat.id}", True, app_config.cachettl_sync_members
    )
    status_message = await message.reply_text(i18n.t("bot.msg.sync_members_start", locale=lang))
    total_members: int | None = None
    current_member_ids: set[int] = set()
    updated = 0
    skipped_existing = 0
    scanned = 0
    partial = False
    stopped = False
    try:
        try:
            total_members = await client.get_chat_members_count(chat.id)
        except Exception as e:
            logger.warning(f"Failed to get members count for chat {chat.id}: {e}")
        existing_snapshots = await database.get_chat_member_snapshot_map(chat.id)
        try:
            current_members = client.get_chat_members(chat.id)
            async for member in current_members:
                member_user = member.user
                if member_user is None or member_user.id is None:
                    continue
                scanned += 1
                current_member_ids.add(member_user.id)
                stored_snapshot = existing_snapshots.get(member_user.id)
                if stored_snapshot is not None and stored_snapshot == _member_snapshot_tuple(member):
                    skipped_existing += 1
                else:
                    snapshot = await database.upsert_member_snapshot(db_chat, member)
                    if snapshot is not None:
                        updated += 1
                if scanned % SYNC_MEMBERS_PROGRESS_INTERVAL == 0:
                    await _safe_edit_sync_status(
                        status_message,
                        _format_sync_members_progress(lang, scanned, updated, total_members, skipped_existing),
                    )
                if operation.stop_requested:
                    stopped = True
                    break
                await asyncio.sleep(SYNC_MEMBERS_SAFE_DELAY_SECONDS)
        except Exception as e:
            logger.error(f"Failed to sync members for chat {chat.id}: {e}")
            error_text = i18n.t("bot.msg.sync_members_error_progress", locale=lang).format(
                scanned=scanned,
                updated=updated,
                error=e.__class__.__name__,
            )
            await _safe_edit_sync_status(status_message, error_text)
            return
        partial = bool(total_members and scanned < total_members)
        if stopped:
            partial = True
        await _safe_edit_sync_status(
            status_message,
            _format_sync_members_progress(lang, scanned, updated, total_members, skipped_existing, partial),
        )
        oks = 0
        if not partial:
            db_associations = await database.get_chat_associations(chat.id)
            db_member_ids = {assoc.user_id for assoc in db_associations}
            to_remove = db_member_ids - current_member_ids
            for user_id in to_remove:
                ok = await database.remove_association(user_id, chat.id)
                if not ok:
                    logger.warning(
                        f"Failed to remove association for user {user_id} in chat {chat.id}"
                    )
                    continue
                oks += 1
                await database.unset_chat_waifus_by_waifu(db_chat, user_id)
        done_text = i18n.t("bot.msg.sync_members_done", locale=lang)
        try:
            done_text = done_text.format(count=oks, scanned=scanned, updated=updated)
        except KeyError:
            done_text = done_text.format(count=oks)
        if skipped_existing:
            done_text += f"\nĐã bỏ qua không đổi: {skipped_existing}"
        if stopped:
            done_text += f"\n🛑 Đã dừng syncmembers theo yêu cầu AI/admin tại {scanned}/{total_members or '?'}; không xoá member chưa thấy."
        elif partial:
            done_text += f"\n⚠️ Chỉ scan được {scanned}/{total_members}; không xoá member chưa thấy để tránh mất dữ liệu."
        await _safe_edit_sync_status(status_message, done_text)
        logger.info(
            f"Synced members for chat {chat.id} ({chat.title}), "
            f"scanned {scanned}, updated {updated}, skipped_existing {skipped_existing}, "
            f"removed {oks} members, partial={partial}"
        )
    finally:
        release_group_member_operation(chat.id, operation)

