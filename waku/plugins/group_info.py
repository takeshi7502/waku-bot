import html
import math

import pyrogram
from pyrogram.errors import BotMethodInvalid, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from waku import database
from waku.config import app_config

_PAGE_SIZE = 10
_CALLBACK_PREFIX = "group_info"


async def _is_bot_admin(user: pyrogram.types.User | None) -> bool:
    if user is None:
        return False
    if user.id in app_config.owners:
        return True
    db_user = await database.get_user_by_id(user.id)
    return bool(db_user and db_user.is_bot_global_admin)


def _format_group_line(index: int, chat) -> str:
    title = html.escape(chat.title or str(chat.id))
    username = f"@{chat.username}" if chat.username else "—"
    return (
        f"<b>{index}. {title}</b>\n"
        f"   🆔 <code>{chat.id}</code>\n"
        f"   🔗 {html.escape(username)}"
    )




async def _filter_accessible_groups(client: pyrogram.Client, groups) -> tuple[list, int]:
    """Return only real Telegram groups the bot can still access."""
    visible = []
    hidden = 0
    me = None
    try:
        me = await client.get_me()
    except Exception:
        pass
    for chat in groups:
        try:
            live_chat = await client.get_chat(chat.id)
            if live_chat.type not in {
                pyrogram.enums.ChatType.GROUP,
                pyrogram.enums.ChatType.SUPERGROUP,
            }:
                hidden += 1
                continue
            if me is not None:
                try:
                    await client.get_chat_member(live_chat.id, me.id)
                except RPCError:
                    hidden += 1
                    continue
            await database.upsert_chat(live_chat)
            visible.append(live_chat)
        except RPCError:
            hidden += 1
        except Exception:
            hidden += 1
    return visible, hidden


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


def _render_info_page(groups, page: int, status_lines: list[str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    total = len(groups)
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)

    lines = status_lines[:] if status_lines else []
    lines.extend(
        [
            "📚 <b>Telegram Groups</b>",
            f"<b>Total</b>: <code>{total}</code>   <b>Page</b>: <code>{page + 1}/{total_pages}</code>",
            "",
        ]
    )
    for index, chat in enumerate(groups[start:end], start=start + 1):
        lines.append(_format_group_line(index, chat))

    return "\n".join(lines), _info_keyboard(page, total_pages)


async def _reply_info_page(
    message: pyrogram.types.Message,
    groups,
    page: int = 0,
    status_lines: list[str] | None = None,
) -> None:
    text, markup = _render_info_page(groups, page, status_lines)
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
    status_lines: list[str] = []

    groups = await database.list_known_telegram_groups(limit=500)
    groups, hidden = await _filter_accessible_groups(client, groups)
    if hidden:
        status_lines.append(f"ℹ️ <b>Đã ẩn</b>: {hidden} group không còn truy cập được hoặc không phải group.")
    if not groups:
        body = "📚 <b>Telegram Groups</b>\n\nChưa có group Telegram nào trong DB."
        await status_msg.edit_text(
            "\n\n".join(status_lines + [body]) if status_lines else body,
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
        return

    text, markup = _render_info_page(groups, 0, status_lines)
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

    text, markup = _render_info_page(groups, int(page_text))
    try:
        if callback_query.message:
            await callback_query.message.edit_text(
                text,
                parse_mode=pyrogram.enums.ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
    except MessageNotModified:
        pass
    await callback_query.answer()
