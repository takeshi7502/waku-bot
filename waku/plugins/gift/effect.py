from pyrogram import filters, types
from pyrogram.client import Client

from waku import affection, common, database, gift
from waku.i18n import i18n
from waku.plugins.agent import state


def _t(locale: str, key: str, **kwargs) -> str:
    return i18n.t(f"bot.msg.gift.{key}", locale=locale).format(**kwargs)


def _gift_line(g) -> str:
    return f"{gift.get_rarity_display_name(g.rarity)} {gift.get_display_name(gift.GiftID(g.gift_id))}"


@Client.on_callback_query(filters.regex(r"^gift:.+:.+$"), group=1)
async def handle_send_gift_callback(
    client: Client, callback_query: types.CallbackQuery
):
    data = callback_query.data
    if data is None:
        return
    data = str(data)
    parts = data.split(":")
    if len(parts) != 3:
        return
    user_id = callback_query.from_user.id
    locale = (await database.get_user_config(user_id)).lang
    gift_db_id_str = parts[1]
    try:
        gift_db_id = int(gift_db_id_str)
    except ValueError:
        return
    offset_str = parts[2]
    try:
        offset = int(offset_str)
    except ValueError:
        offset = 0
    user_data = await database.get_user_by_id(user_id)
    if not user_data:
        await callback_query.answer(_t(locale, "user_not_found"), show_alert=True)
        return
    gift_item = await database.get_gift_by_db_id(gift_db_id)
    if not gift_item:
        await callback_query.answer(_t(locale, "gift_not_found"), show_alert=True)
        return
    if gift_item.owner_id != user_data.id:
        await callback_query.answer(_t(locale, "not_your_gift"), show_alert=True)
        return
    if gift_item.sent_to_bot:
        await callback_query.answer(_t(locale, "already_sent"), show_alert=True)
        return
    gift_id = gift.GiftID(gift_item.gift_id)
    gift_def = gift.get_gift_by_id(gift_id)
    display_name = gift.get_display_name(gift_def.id)
    # maybe a verge ugly match case...
    match gift_def.id:
        case gift.GiftID.SEVERED_GRASS_SILENCE:
            # clear bot agent memory
            await common.memttlcache.delete(f"agent_user_memory:{user_id}")
        case gift.GiftID.VOW_LOTUS_SEAL:
            # prevent affection
            duration = gift_def.effects.get("duration", 0) * gift_item.rarity
            passivation = gift_def.effects.get("passivation", 0) * gift_item.rarity
            await common.memttlcache.set(
                f"affection_passivation:{user_id}", passivation, duration
            )
        case gift.GiftID.AMARANTH_HEART_LAMP:
            current = await affection.get_user_affection(user_id)
            add_affection = gift_def.effects.get("add_affection", 0) * gift_item.rarity
            duration = gift_def.effects.get("duration", 0) * gift_item.rarity
            await affection.set_user_temporary_affection(
                user_id=user_id,
                affection=current + add_affection,
                ttl=duration,
            )
        case gift.GiftID.FROST_FLOWER_WHISPER:
            # show memory and affection
            memory = await common.memttlcache.get(f"agent_user_memory:{user_id}", None)
            affection_rank = await affection.get_affection_rank(user_id)
            memory_text = memory if memory is not None else _t(locale, "no_memory")
            await callback_query.message.reply_text(
                _t(locale, "memory_status", memory=memory_text, rank=affection_rank)
            )
        case gift.GiftID.DAWN_BELL_HERB:
            await common.memttlcache.delete(state.user_blocked_key(user_id))
            immune_duration = gift_def.effects.get("immune_duration", 0) * gift_item.rarity
            if immune_duration > 0:
                await common.memttlcache.set(
                    state.user_block_immune_key(user_id), True, ttl=immune_duration
                )
        case _:
            await callback_query.answer(_t(locale, "strange_gift"), show_alert=True)
            return
    # common effects
    affection_change = gift_def.effects.get("affection_change", 0)
    if affection_change != 0:
        await affection.update_user_affection(
            user_id=user_id,
            change=affection_change,
        )
    await database.mark_gift_as_sent(gift_db_id)
    await callback_query.answer(
        _t(
            locale,
            "send_success",
            rarity=gift.get_rarity_display_name(gift_item.rarity),
            name=display_name,
        ),
        show_alert=True,
    )
    user_gifts = await database.get_user_gifts(user_id, False, offset, 5)
    user_gifts_total = await database.count_user_gifts(user_id, False)
    if not user_gifts:
        if user_gifts_total == 0:
            await callback_query.edit_message_text(_t(locale, "all_sent"))
            return
        else:
            offset = max(0, offset - 5)
            user_gifts = await database.get_user_gifts(user_id, False, offset, 5)
    text = _t(locale, "send_more_prompt")
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
