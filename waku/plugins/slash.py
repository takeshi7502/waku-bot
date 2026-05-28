import html
import random
import re

from pyrogram import Client, filters
from pyrogram.enums import ChatType, MessageEntityType, ParseMode
from pyrogram.types import LinkPreviewOptions, Message

from waku import common, database
from waku.common.utils import is_explicit_reply
from waku.config import app_config
from waku.i18n import i18n


def _replace_char(text: str):
    text = text.replace("$", "").replace("/", "").replace("\\", "")
    return text


async def slash_fliter_func(_, __, message: Message) -> bool:
    if not message.text:
        return False
    if len(message.text) <= 1:
        return False
    if (
        message.entities is not None
        and message.entities[0].type == MessageEntityType.BOT_COMMAND
    ):
        return False
    if message.text.startswith("/") or message.text.startswith("\\"):
        return True
    return False


slash_fliter = filters.create(slash_fliter_func)


async def _get_message_locale(message: Message) -> str:
    if message.chat and message.chat.type != ChatType.PRIVATE:
        chat_config = await database.get_chat_config(message.chat)
        return chat_config.lang
    if message.from_user:
        user_config = await database.get_user_config(message.from_user)
        return user_config.lang
    return app_config.lang


def _tr(locale: str, key: str, **kwargs) -> str:
    return i18n.t(f"bot.msg.slash.{key}", locale=locale).format(**kwargs)


@Client.on_message(slash_fliter, group=0)
async def slash(client: Client, message: Message):
    if not message.text:
        return
    if message.text.startswith("/"):
        if not message.text.startswith("//"):
            if re.match(r"^/[a-zA-Z0-9]+", message.text):
                return
    cmd1 = ""
    cmd2 = ""
    text = ""
    this_user = message.sender_chat or message.from_user
    this_mention = await common.mention_html(this_user)
    locale = await _get_message_locale(message)
    replied_user = None
    replied_mention = ""
    if is_explicit_reply(message) and message.reply_to_message:
        reply_to_message = message.reply_to_message
        replied_user = reply_to_message.sender_chat or reply_to_message.from_user
        if replied_user:
            replied_mention = await common.mention_html(replied_user)
    is_reverse = (
        False
        if message.text.startswith("/")
        else True
        if message.text.startswith("\\")
        else False
    )
    is_one_cmd = len(message.text.split(" ")) == 1
    cmd1 = html.escape(_replace_char(message.text.split(" ")[0][1:]))
    if not cmd1:
        return
    if not is_one_cmd:
        cmd2 = html.escape(_replace_char(" ".join(message.text.split(" ")[1:])))
        if is_reverse:
            text = (
                _tr(
                    locale,
                    "reverse_with_arg",
                    actor=this_mention,
                    target=replied_mention,
                    action=cmd1,
                    extra=cmd2,
                )
                if replied_user
                else _tr(
                    locale,
                    "self_with_arg",
                    actor=this_mention,
                    action=cmd1,
                    extra=cmd2,
                )
            )
        else:
            text = (
                _tr(
                    locale,
                    "forward_with_arg",
                    actor=this_mention,
                    target=replied_mention,
                    action=cmd1,
                    extra=cmd2,
                )
                if replied_user
                else _tr(
                    locale,
                    "self_with_arg",
                    actor=this_mention,
                    action=cmd1,
                    extra=cmd2,
                )
            )
    else:
        if is_reverse:
            text = (
                _tr(locale, "reverse", actor=this_mention, target=replied_mention, action=cmd1)
                if replied_user
                else _tr(locale, "self_reverse", actor=this_mention, action=cmd1)
            )
        else:
            text = (
                _tr(locale, "forward", actor=this_mention, target=replied_mention, action=cmd1)
                if replied_user
                else _tr(locale, "self", actor=this_mention, action=cmd1)
            )
    text = re.sub(r"([a-zA-Z0-9])([\u4e00-\u9fa5])", r"\1 \2", text)
    text = re.sub(r"([\u4e00-\u9fa5])([a-zA-Z0-9])", r"\1 \2", text)
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    if random.uniform(0, 1) < app_config.coin_add_chance_on_slash:
        coins = 16 * random.randint(1, 4)
        await database.add_user_coins(this_user.id, coins)
    if replied_user:
        if random.uniform(0, 1) < app_config.coin_add_chance_on_be_slash:
            coins = 16 * random.randint(1, 4)
            await database.add_user_coins(replied_user.id, coins)
