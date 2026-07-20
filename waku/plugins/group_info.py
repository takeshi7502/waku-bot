import html
import math

import pyrogram
from pyrogram.errors import BotMethodInvalid, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from waku import database
from waku.common.memory_store import memttlcache
from waku.config import app_config

_PAGE_SIZE = 10
_CALLBACK_PREFIX = "group_info"
_STATUS_CACHE_TTL = 15 * 60


def _status_cache_key(message_id: int) -> str:
    return f"group_info:statuses:{message_id}"


async def _is_bot_admin(user: pyrogram.types.User | None) -> bool:
    if user is None:
        return False
    if user.id in app_config.owners:
        return True
    db_user = await database.get_user_by_id(user.id)
    return bool(db_user and db_user.is_bot_global_admin)


def _format_group_line(index: int, chat, is_accessible: bool) -> str:
    title = html.escape(chat.title or str(chat.id))
    username = f"@{chat.username}" if chat.username else "—"
    status_icon = "🟢" if is_accessible else "🔴"
    return (
        f"<blockquote><b>{index}. {title}</b></blockquote>\n"
        f"   <b>ID:</b> <code>{chat.id}</code>\n"
        f"   <b>Link:</b> {html.escape(username)} | {status_icon}"
    )


async def _get_group_access_statuses(
    client: pyrogram.Client, groups
) -> dict[int, bool]:
    """Check current access without hiding any group stored in the database."""
    statuses: dict[int, bool] = {}
    me = None
    try:
        me = await client.get_me()
    except Exception:
        pass

    for chat in groups:
        is_accessible = False
        try:
            live_chat = await client.get_chat(chat.id)
            if live_chat.type in {
                pyrogram.enums.ChatType.GROUP,
                pyrogram.enums.ChatType.SUPERGROUP,
            }:
                is_accessible = True
                if me is not None:
                    try:
                        await client.get_chat_member(live_chat.id, me.id)
                    except RPCError:
                        is_accessible = False
                if is_accessible:
                    await database.upsert_chat(live_chat)
        except RPCError:
            pass
        except Exception:
            pass
        statuses[chat.id] = is_accessible

    return statuses


async def _refresh_known_groups(client: pyrogram.Client) -> tuple[int, str | None]:
    """Refresh known group rows from dialogs when the current session supports it."""
    refreshed = 0
    try:
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat.type not in {
                pyrogram.enums.ChatType.GROUP,
                pyrogram.enums.ChatType.SUPERGROUP,
            }:
                continue
            await database.upsert_chat(chat)
            refreshed += 1
    except BotMethodInvalid:
        return (
            refreshed,
            "Telegram không cho bot account quét toàn bộ dialog. "
            "Waku vẫn hiện group đã lưu; group cũ chưa lưu cần có message/command trong group để cập nhật.",
        )
    return refreshed, None


def _info_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️", callback_data=f"{_CALLBACK_PREFIX}:p:{page - 1}"))
    buttons.append(InlineKeyboardButton("✖️", callback_data=f"{_CALLBACK_PREFIX}:x:{page}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("▶️", callback_data=f"{_CALLBACK_PREFIX}:p:{page + 1}"))
    return InlineKeyboardMarkup([buttons])


def _render_info_page(
    groups,
    page: int,
    access_statuses: dict[int, bool],
    status_lines: list[str] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(groups)
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)

    lines = status_lines[:] if status_lines else []
    lines.extend(
        [
            "📚 <b>Telegram Groups</b>",
            f"<b>Total:</b> <code>{total}</code>   <b>Page:</b> <code>{page + 1}/{total_pages}</code>",
            "",
        ]
    )
    for index, chat in enumerate(groups[start:end], start=start + 1):
        lines.append(
            _format_group_line(index, chat, access_statuses.get(chat.id, False))
        )

    return "\n".join(lines), _info_keyboard(page, total_pages)


async def _reply_info_page(
    message: pyrogram.types.Message,
    groups,
    access_statuses: dict[int, bool],
    page: int = 0,
    status_lines: list[str] | None = None,
) -> None:
    text, markup = _render_info_page(groups, page, access_statuses, status_lines)
    await message.reply_text(
        text,
        parse_mode=pyrogram.enums.ParseMode.HTML,
        quote=False,
        disable_web_page_preview=True,
        reply_markup=markup,
    )


@pyrogram.Client.on_message(
    pyrogram.filters.command("info") & pyrogram.filters.private, group=0
)
async def info_command(client: pyrogram.Client, message: pyrogram.types.Message):
    if not await _is_bot_admin(message.from_user):
        await message.reply_text(
            "Chỉ bot admin mới dùng được lệnh này trong DM.",
            quote=False,
        )
        return

    status_msg = await message.reply_text(
        "🔎 Đang kiểm tra danh sách group...",
        quote=False,
    )
    groups = await database.list_known_telegram_groups(limit=500)
    access_statuses = await _get_group_access_statuses(client, groups)
    if not groups:
        body = "📚 <b>Telegram Groups</b>\n\nChưa có group Telegram nào trong DB."
        await status_msg.edit_text(
            body,
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
        return

    await memttlcache.set(
        _status_cache_key(status_msg.id),
        access_statuses,
        ttl=_STATUS_CACHE_TTL,
    )
    text, markup = _render_info_page(groups, 0, access_statuses)
    await status_msg.edit_text(
        text,
        parse_mode=pyrogram.enums.ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=markup,
    )


@pyrogram.Client.on_callback_query(pyrogram.filters.regex(rf"^{_CALLBACK_PREFIX}:"))
async def info_callback(client: pyrogram.Client, callback_query: pyrogram.types.CallbackQuery):
    if not await _is_bot_admin(callback_query.from_user):
        await callback_query.answer("Không có quyền.", show_alert=True)
        return

    data = callback_query.data or ""
    parts = data.split(":", maxsplit=2)
    if len(parts) != 3:
        await callback_query.answer("Nút không hợp lệ.", show_alert=True)
        return

    action, page_text = parts[1], parts[2]
    if action == "x":
        if callback_query.message:
            await memttlcache.delete(_status_cache_key(callback_query.message.id))
            await callback_query.message.delete()
        await callback_query.answer()
        return

    if action != "p" or not page_text.isdigit():
        await callback_query.answer("Nút không hợp lệ.", show_alert=True)
        return

    groups = await database.list_known_telegram_groups(limit=500)
    if not groups:
        await callback_query.answer("Không còn group nào trong DB.", show_alert=True)
        return

    callback_message = callback_query.message
    if callback_message is None:
        await callback_query.answer("Message không còn tồn tại.", show_alert=True)
        return

    access_statuses = await memttlcache.get(
        _status_cache_key(callback_message.id)
    )
    if access_statuses is None:
        await callback_query.answer(
            "Trạng thái đã hết hạn, hãy gọi lại /info.", show_alert=True
        )
        return

    text, markup = _render_info_page(
        groups, int(page_text), access_statuses
    )
    try:
        await callback_message.edit_text(
            text,
            parse_mode=pyrogram.enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except MessageNotModified:
        pass
    await callback_query.answer()
