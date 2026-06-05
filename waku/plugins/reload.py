import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pyrogram
from pyrogram.client import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from waku import database
from waku.config import app_config, reload_config
from waku.logger import logger

_RESTART_DELAY_SECONDS = 2
_PENDING_RELOAD_STATUS_PATH = Path("data/pending_reload_status.json")
_RELOAD_CALLBACK_PREFIX = "reload_cfg"


def _format_changed_fields(changed: list[str]) -> str:
    if not changed:
        return "<i>Không có field nào thay đổi.</i>"
    return "\n".join(f"• <code>{field}</code>" for field in changed)


def _reload_status_text(status: str, changed: list[str], footer: str) -> str:
    icon = "✅" if "success" in status.lower() else "🔄"
    changed_count = f"{len(changed)} fields" if changed else "No fields changed"
    return "\n".join(
        [
            f"<b>{icon} Waku Reload</b>",
            "",
            f"<b>Status:</b> <code>{status}</code>",
            f"<b>Changed:</b> <code>{changed_count}</code>",
            "",
            _format_changed_fields(changed),
            "",
            f"<i>{footer}</i>",
        ]
    )


def _reload_confirm_text(user_id: int) -> str:
    return "\n".join(
        [
            "<b>🔄 Xác nhận reload</b>",
            "",
            "Reload sẽ đọc lại config và restart bot để áp dụng runtime settings.",
            f"<code>User ID: {user_id}</code>",
            "",
            "Bấm ✅ để tiếp tục hoặc ❌ để huỷ.",
        ]
    )


def _reload_confirm_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅", callback_data=f"{_RELOAD_CALLBACK_PREFIX} confirm {user_id}"
                ),
                InlineKeyboardButton(
                    "❌", callback_data=f"{_RELOAD_CALLBACK_PREFIX} cancel {user_id}"
                ),
            ]
        ]
    )


async def _save_pending_reload_status(
    reply: pyrogram.types.Message,
    user_id: int,
    changed: list[str],
) -> None:
    _PENDING_RELOAD_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chat_id": reply.chat.id,
        "message_id": reply.id,
        "user_id": user_id,
        "changed": changed,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _PENDING_RELOAD_STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def complete_pending_reload_status(client: Client) -> None:
    if not _PENDING_RELOAD_STATUS_PATH.exists():
        return
    try:
        payload = json.loads(_PENDING_RELOAD_STATUS_PATH.read_text(encoding="utf-8"))
        changed = list(payload.get("changed") or [])
        await client.edit_message_text(
            chat_id=payload["chat_id"],
            message_id=payload["message_id"],
            text=_reload_status_text(
                "Restarted successfully",
                changed,
                "Runtime settings are now active.",
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(
            "Completed pending /reload status message: "
            f"chat={payload['chat_id']} message={payload['message_id']}"
        )
    except Exception as e:
        logger.warning(f"Failed to complete pending /reload status: {e.__class__.__name__}: {e}")
    finally:
        try:
            _PENDING_RELOAD_STATUS_PATH.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"Failed to delete pending /reload status file: {e}")


async def _exit_after_reload(user_id: int) -> None:
    await asyncio.sleep(_RESTART_DELAY_SECONDS)
    logger.warning(f"Restarting process after /reload by {user_id}")
    os._exit(0)


def _is_authorized_reload_user(
    query: pyrogram.types.CallbackQuery, expected_user_id: int
) -> bool:
    return query.from_user is not None and query.from_user.id == expected_user_id


@Client.on_message(pyrogram.filters.command("reload"), group=0)
async def reload_command(client: Client, message: pyrogram.types.Message):
    user = message.from_user
    if user is None:
        return
    db_user = await database.get_user_by_id(user.id)
    if not db_user:
        return
    if not db_user.is_bot_global_admin and user.id not in app_config.owners:
        await message.reply_text("Permission denied")
        return

    await message.reply_text(
        _reload_confirm_text(user.id),
        reply_markup=_reload_confirm_markup(user.id),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        quote=True,
    )


@Client.on_callback_query(
    pyrogram.filters.regex(rf"^{_RELOAD_CALLBACK_PREFIX}\s+(confirm|cancel)\s+\d+$"),
    group=0,
)
async def reload_callback(client: Client, query: pyrogram.types.CallbackQuery):
    if query.message is None or query.data is None:
        return
    data = str(query.data).split()
    action = data[1]
    expected_user_id = int(data[2])
    if not _is_authorized_reload_user(query, expected_user_id):
        await query.answer("Nút này không dành cho bạn.", show_alert=True, cache_time=30)
        return

    if action == "cancel":
        try:
            await query.message.edit_text(
                "<b>❌ Waku Reload</b>\n\n<i>Đã huỷ reload.</i>",
                reply_markup=None,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except MessageNotModified:
            pass
        await query.answer("Đã huỷ")
        return

    await query.answer("Đang reload...")
    success, msg, changed = reload_config()
    if not success:
        await query.message.edit_text(
            f"<b>❌ Waku Reload</b>\n\n<code>{msg}</code>",
            reply_markup=None,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if changed:
        logger.info(
            f"Config reload validated by {expected_user_id}, changed fields: {', '.join(changed)}"
        )
    await query.message.edit_text(
        _reload_status_text(
            "Restarting...",
            changed,
            "Bot is restarting to apply runtime settings.",
        ),
        reply_markup=None,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await _save_pending_reload_status(query.message, expected_user_id, changed)
    asyncio.create_task(_exit_after_reload(expected_user_id))
