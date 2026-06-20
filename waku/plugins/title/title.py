import asyncio
import html
import json

import pyrogram.errors
from pyrogram.client import Client as PyrogramClient

from waku import common, database, i18n
from waku.common.tgmethod import mention_html
from waku.common.utils import get_reply_target
from waku.logger import logger

from . import utils

_SAVE_AUTO_DELETE_SECONDS = 5


async def _delete_message_later(
    message: pyrogram.types.Message | None,
    delay: int = _SAVE_AUTO_DELETE_SECONDS,
) -> None:
    if message is None:
        return
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


@PyrogramClient.on_message(
    pyrogram.filters.command("t") & pyrogram.filters.group, group=0
)
async def set_member_title(client: PyrogramClient, message: pyrogram.types.Message):
    chat = message.chat
    user = message.sender_chat or message.from_user
    if not user or not chat or not chat.id:
        return
    reply_target = get_reply_target(message)
    target = reply_target.from_user if reply_target else user
    if not target or not target.id or isinstance(target, pyrogram.types.Chat):
        await message.reply_text(
            i18n.t(
                "bot.msg.title.errors.user_id_invalid",
                locale=(await database.get_chat_config(chat)).lang,
            ),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
        return
    if not message.command:
        return
    custom_title = " ".join(message.command[1:]).strip()
    if not custom_title:
        custom_title = target.username or target.full_name
    chat_config = await database.get_chat_config(chat.id)
    permissions = chat_config.title_permissions or {}
    if isinstance(permissions, str):
        logger.warning(f"Chat {chat.id} has title_permissions as string: {permissions}")
        permissions = json.loads(permissions)
    try:
        text = (
            i18n.t("bot.msg.title.set_self", locale=chat_config.lang).format(
                title=html.escape(custom_title),
            )
            if target.id == user.id
            else i18n.t("bot.msg.title.set_other", locale=chat_config.lang).format(
                title=html.escape(custom_title),
                target=target.mention(style="html"),
                user=(await mention_html(user)),
            )
        )
        # Check if the target user is an administrator or creator
        target_member = await common.get_chat_member(client, chat.id, target.id)
        is_admin = target_member.status in (
            pyrogram.enums.ChatMemberStatus.ADMINISTRATOR,
            pyrogram.enums.ChatMemberStatus.OWNER,
        )
        if is_admin:
            await client.set_administrator_title(chat.id, target.id, custom_title)
        else:
            await client.set_chat_member_tag(
                chat.id,
                target.id,
                tag=custom_title,
            )
        await message.reply_text(text, parse_mode=pyrogram.enums.ParseMode.HTML)
    except pyrogram.errors.UserCreator:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.creator", locale=chat_config.lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.ChatAdminRequired:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.admin_required", locale=chat_config.lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.AdminRankInvalid:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.rank_invalid", locale=chat_config.lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.UserIdInvalid:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.user_id_invalid", locale=chat_config.lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.RightForbidden:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.right_forbidden", locale=chat_config.lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Error setting title: {e}")
        await message.reply_text(
            f"{i18n.t('bot.msg.title.errors.generic', locale=chat_config.lang)}\n<code>{e}</code>",
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )


@PyrogramClient.on_message(
    pyrogram.filters.command("sett") & pyrogram.filters.group, group=0
)
async def set_title_permissions(
    client: PyrogramClient, message: pyrogram.types.Message
):
    chat = message.chat
    user = message.sender_chat or message.from_user
    if chat is None or chat.id is None or user is None:
        return
    chat_config = await database.get_chat_config(chat.id)
    if not await common.can_user_manage_bot_in_chat(user, chat, "change_info"):
        await message.reply_text(
            i18n.t("bot.msg.no_permission_group", locale=chat_config.lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
        return
    await message.reply_text(
        i18n.t("bot.msg.title.set_permissions", locale=chat_config.lang),
        parse_mode=pyrogram.enums.ParseMode.HTML,
        reply_markup=utils.TitlePermissionsMarkup(
            chat_config.title_permissions or {}, chat_config.lang
        ).build(),
    )


@PyrogramClient.on_callback_query(
    pyrogram.filters.regex(r"^set_title_permissions\s+(toggle\s+\w+|save)$"),
    group=0,
)
async def set_title_permissions_callback(
    client: PyrogramClient,
    query: pyrogram.types.CallbackQuery,
):
    chat = query.message.chat
    user = query.from_user
    if chat is None or chat.id is None or user is None:
        return
    chat_config = await database.get_chat_config(chat.id)
    if not await common.can_user_manage_bot_in_chat(user, chat, "change_info"):
        await query.answer(
            i18n.t("bot.msg.no_permission_group", locale=chat_config.lang),
            show_alert=True,
            cache_time=60,
        )
        return
    data = str(query.data).split()
    action = data[1]
    if action == "save":
        await query.message.edit_text(
            i18n.t("bot.msg.group_config_saved", locale=chat_config.lang),
            reply_markup=None,
        )
        asyncio.create_task(_delete_message_later(query.message))
        asyncio.create_task(_delete_message_later(query.message.reply_to_message))
        return
    if action != "toggle" or len(data) < 3:
        await query.answer(
            i18n.t("bot.msg.unknown_operation", locale=chat_config.lang),
            show_alert=True,
        )
        return
    permission = data[2]
    permissions = chat_config.title_permissions or {}
    if isinstance(permissions, str):
        permissions = json.loads(permissions)
    permissions[permission] = not permissions.get(permission, False)
    chat_config.title_permissions = permissions
    await database.update_chat_config(chat.id, chat_config)
    await query.message.edit_reply_markup(
        utils.TitlePermissionsMarkup(permissions, chat_config.lang).build()
    )


@PyrogramClient.on_message(
    pyrogram.filters.command("td") & pyrogram.filters.group, group=0
)
async def delete_member_title(client: PyrogramClient, message: pyrogram.types.Message):
    chat = message.chat
    user = message.from_user
    if chat is None or chat.id is None or user is None:
        return
    lang = (await database.get_chat_config(chat)).lang
    if not user or not chat:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.no_chat_or_user", locale=lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
        return
    try:
        me = await common.get_chat_member(client, chat.id, "me")
        if (not me.status == pyrogram.enums.ChatMemberStatus.ADMINISTRATOR) or (
            not me.privileges.can_promote_members
        ):
            if not me.privileges.can_manage_tags:
                await message.reply_text(
                    i18n.t("bot.msg.title.errors.admin_required", locale=lang),
                    parse_mode=pyrogram.enums.ParseMode.HTML,
                )
                return
            await client.set_chat_member_tag(
                chat.id,
                user.id,
                tag=None,
            )
            await message.reply_text(
                i18n.t("bot.msg.title.deleted", locale=lang),
                parse_mode=pyrogram.enums.ParseMode.HTML,
            )
            return
        await client.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            privileges=pyrogram.types.ChatAdministratorRights(can_manage_chat=False),
        )
        if me.privileges.can_manage_tags:
            await client.set_chat_member_tag(chat.id, user.id, tag=None)
        await message.reply_text(
            i18n.t("bot.msg.title.deleted", locale=lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.UserCreator:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.creator", locale=lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.ChatAdminRequired:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.admin_required", locale=lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.AdminRankInvalid:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.rank_invalid", locale=lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except pyrogram.errors.UserIdInvalid:
        await message.reply_text(
            i18n.t("bot.msg.title.errors.user_id_invalid", locale=lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Error deleting title: {e}")
        await message.reply_text(
            i18n.t("bot.msg.title.errors.generic", locale=lang),
            parse_mode=pyrogram.enums.ParseMode.HTML,
        )
