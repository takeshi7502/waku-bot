import random

from pyrogram import enums, filters, types
from pyrogram.client import Client

from waku import database, gift
from waku.i18n import i18n


def _t(locale: str, key: str, **kwargs) -> str:
    return i18n.t(f"bot.msg.gift.{key}", locale=locale).format(**kwargs)


def _gift_line(g) -> str:
    return f"{gift.get_rarity_display_name(g.rarity)} {gift.get_display_name(gift.GiftID(g.gift_id))}"


def _gift_buttons(available_gifts, user_id: int):
    return [
        [
            types.InlineKeyboardButton(
                gift.get_display_name(g.id),
                callback_data=f"buygift:{user_id}:{g.id}:req",
            )
            for g in available_gifts
        ]
    ]


@Client.on_message(filters.command("buygift") & filters.private, group=1)
async def buy_gift(client: Client, message: types.Message):
    user = message.from_user
    if user is None:
        return
    user_data = await database.get_user_by_id(user.id)
    if not user_data:
        return
    locale = user_data.user_config.lang
    user_coin = user_data.user_config.coins
    affordable_gifts = gift.list_affordable_gifts(user_coin)
    if not affordable_gifts:
        await message.reply_text(_t(locale, "none_available"))
        return
    await message.reply_text(
        _t(locale, "buy_prompt"),
        reply_markup=types.InlineKeyboardMarkup(
            [
                *_gift_buttons(affordable_gifts, user.id),
                [
                    types.InlineKeyboardButton(
                        _t(locale, "leave"),
                        callback_data="delete_callback_query_message",
                    )
                ],
            ]
        ),
    )


@Client.on_callback_query(filters.regex(r"^buygift:(\d+):(.+):(.+)$"), group=0)
async def handle_buy_gift_callback(client: Client, callback_query: types.CallbackQuery):
    data = callback_query.data
    if data is None:
        return
    data = str(data)
    parts = data.split(":")
    if len(parts) != 4:
        return
    user_id_str, gift_id_str, status = parts[1], parts[2], parts[3]
    try:
        user_id = int(user_id_str)
    except ValueError:
        return
    user_config = await database.get_user_config(callback_query.from_user.id)
    locale = user_config.lang
    if callback_query.from_user.id != user_id:
        await callback_query.answer(_t(locale, "not_your_purchase"), show_alert=True)
        return
    gift_id = gift.GiftID(gift_id_str)
    gift_item = gift.get_gift_by_id(gift_id)
    user_data = await database.get_user_by_id(user_id)
    if not user_data:
        await callback_query.answer(_t(locale, "user_not_found"), show_alert=True)
        return
    user_coins = user_data.user_config.coins
    match status:
        case "yes":
            if user_data.user_config.coins < gift_item.price:
                await callback_query.answer(
                    _t(locale, "not_enough_coins"), show_alert=True
                )
                return
            rarity = random.randint(1, 5)
            await database.buy_gift_for_user(user_id, gift_id, rarity=rarity)
            await callback_query.answer(
                _t(
                    locale,
                    "buy_success",
                    rarity=gift.get_rarity_display_name(rarity),
                    name=gift.get_display_name(gift_item.id),
                ),
                show_alert=True,
            )
            user_coins_after = (await database.get_user_config(user_id)).coins
            percent_now = gift_item.price * 100 // user_coins_after
            if percent_now > 100 or user_coins <= 0:
                percent_now = 100
            await callback_query.edit_message_text(
                _t(
                    locale,
                    "buy_again",
                    name=gift.get_display_name(gift_item.id),
                    percent=percent_now,
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    [
                        [
                            types.InlineKeyboardButton(
                                _t(locale, "buy_one_more"),
                                callback_data=f"buygift:{user_id}:{gift_id_str}:yes",
                            ),
                            types.InlineKeyboardButton(
                                _t(locale, "leave"),
                                callback_data="delete_callback_query_message",
                            ),
                        ]
                    ]
                ),
            )
        case "no":
            affordable_gifts = gift.list_affordable_gifts(user_coins)
            if not affordable_gifts:
                await callback_query.edit_message_text(
                    _t(locale, "cancelled_no_gifts"),
                    reply_markup=None,  # type: ignore
                )
                return
            await callback_query.edit_message_text(
                _t(locale, "cancelled_prompt"),
                reply_markup=types.InlineKeyboardMarkup(
                    [
                        *_gift_buttons(affordable_gifts, user_data.id),
                        [
                            types.InlineKeyboardButton(
                                _t(locale, "leave"),
                                callback_data="delete_callback_query_message",
                            )
                        ],
                    ]
                ),
            )
        case "req":
            price_percent = (
                int((gift_item.price / user_coins) * 100) if user_coins > 0 else 0
            )
            if price_percent > 100:
                price_percent = 100
            display_name = gift.get_display_name(gift_item.id)
            text = _t(
                locale,
                "confirm_buy",
                name=display_name,
                description=gift_item.description,
                comment=gift_item.comment,
                percent=price_percent,
            )
            await callback_query.message.edit_text(
                text=text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=types.InlineKeyboardMarkup(
                    [
                        [
                            types.InlineKeyboardButton(
                                _t(locale, "confirm"),
                                callback_data=f"buygift:{user_id}:{gift_id_str}:yes",
                            ),
                            types.InlineKeyboardButton(
                                _t(locale, "cancel"),
                                callback_data=f"buygift:{user_id}:{gift_id_str}:no",
                            ),
                        ]
                    ]
                ),
            )
        case _:
            await callback_query.answer(_t(locale, "unknown_operation"), show_alert=True)
            return


@Client.on_message(filters.command("gift") & filters.private, group=1)
async def send_gift(client: Client, message: types.Message):
    user = message.from_user
    if user is None:
        return
    user_data = await database.get_user_by_id(user.id)
    if not user_data:
        return
    locale = user_data.user_config.lang
    user_gifts = await database.get_user_gifts(user.id, False, 0, 5)
    if not user_gifts:
        await message.reply_text(_t(locale, "no_owned_gifts"))
        return
    user_gifts_total = await database.count_user_gifts(user.id, False)
    text = _t(locale, "send_prompt")
    for i, g in enumerate(user_gifts, start=1):
        text += f"\n{i}. {_gift_line(g)}"
    buttons = [
        [
            types.InlineKeyboardButton(
                str(i + 1),
                callback_data=f"gift:{g.id}:0",
            )
            for i, g in enumerate(user_gifts)
        ]
    ]
    if user_gifts_total >= 5:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    i18n.t("bot.button.page_prev", locale=locale),
                    callback_data="sendgift_page:-5",
                ),
                types.InlineKeyboardButton(
                    i18n.t("bot.button.page_next", locale=locale),
                    callback_data="sendgift_page:5",
                ),
            ]
        )
    await message.reply_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^sendgift_page:.+$"), group=0)
async def handle_send_gift_page_callback(
    client: Client, callback_query: types.CallbackQuery
):
    data = callback_query.data
    if data is None:
        return
    data = str(data)
    parts = data.split(":")
    if len(parts) != 2:
        return
    offset_str = parts[1]
    try:
        offset = int(offset_str)
    except ValueError:
        return
    user_id = callback_query.from_user.id
    user_data = await database.get_user_by_id(user_id)
    user_config = await database.get_user_config(user_id)
    locale = user_config.lang
    if offset < 0:
        await callback_query.answer(_t(locale, "no_more"), show_alert=True)
        return
    if not user_data:
        await callback_query.answer(_t(locale, "user_not_found"), show_alert=True)
        return
    user_gifts = await database.get_user_gifts(user_id, False, offset, 5)
    if not user_gifts:
        await callback_query.answer(_t(locale, "no_more_gifts"), show_alert=True)
        return
    user_gifts_total = await database.count_user_gifts(user_id, False)
    text = _t(locale, "send_prompt")
    for i, g in enumerate(user_gifts, start=1 + offset):
        text += f"\n{i}. {_gift_line(g)}"
    buttons = [
        [
            types.InlineKeyboardButton(
                str(i + 1 + offset),
                callback_data=f"gift:{g.id}:{offset}",
            )
            for i, g in enumerate(user_gifts)
        ]
    ]
    if user_gifts_total >= 5:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    i18n.t("bot.button.page_prev", locale=locale),
                    callback_data=f"sendgift_page:{offset - 5}",
                ),
                types.InlineKeyboardButton(
                    i18n.t("bot.button.page_next", locale=locale),
                    callback_data=f"sendgift_page:{offset + 5}",
                ),
            ]
        )
    await callback_query.edit_message_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(buttons),
    )
