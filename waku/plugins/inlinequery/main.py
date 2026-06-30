import asyncio
import html
import random
from io import BytesIO

from pyrogram import enums, types
from pyrogram.client import Client

from waku import database, i18n
from waku.common.memory_store import memttlcache
from waku.config import app_config
from waku.logger import logger
from waku.plugins.inlinequery.manomeme import handle_manomeme
from waku.services.manyacg import manyacg_client

from . import hack, manomeme
from .quote import query_quote


@Client.on_inline_query()
async def inline_query_handler(client: Client, query: types.InlineQuery):
    user = query.from_user
    user_config = await database.get_user_config(user)
    datas = query.query.strip().split(" ")
    if not datas or datas[0] == "":
        results: list[types.InlineQueryResult] = []
        if manyacg_client:
            try:
                resp = await manyacg_client.random_artwork(limit=1, r18=2)
                if resp.status == 200 and resp.data and resp.data[0].pictures:
                    artwork = resp.data[0]
                    picture = artwork.pictures[
                        random.randint(0, len(artwork.pictures) - 1)
                    ]
                    results.append(
                        types.InlineQueryResultPhoto(
                            id=f"seg_{picture.id}",
                            photo_url=picture.regular,
                            thumb_url=picture.regular,
                            title="Seg free",
                            description="Gửi một ảnh anime/Pixiv ngẫu nhiên",
                            caption=f"<a href='{artwork.source_url}'>{html.escape(artwork.title)}</a>",
                            parse_mode=enums.ParseMode.HTML,
                            reply_markup=types.InlineKeyboardMarkup(
                                [
                                    [
                                        types.InlineKeyboardButton(
                                            text=i18n.t(
                                                "bot.button.manyacg.detail",
                                                locale=user_config.lang,
                                            ),
                                            url=artwork.source_url,
                                        ),
                                        types.InlineKeyboardButton(
                                            text=i18n.t(
                                                "bot.button.manyacg.original",
                                                locale=user_config.lang,
                                            ),
                                            url=f"https://t.me/{app_config.manyacg_bot}/?start=file_{picture.id}",
                                        ),
                                    ]
                                ]
                            ),
                        )
                    )
            except Exception as e:
                logger.error(f"inline seg query error: {e.__class__.__name__}:{e}")
        bottle = await database.pick_random_bottle()
        if bottle is not None:
            bottle_text = bottle.text or ""
            bottle_description = (
                bottle_text[:80]
                if bottle_text
                else i18n.t("bot.inline.pick_bottle_description", locale=user_config.lang)
            )
            db_user = await database.get_user_by_id(user.id)
            bot_username = client.me.username if client.me else None
            row1 = [
                types.InlineKeyboardButton(
                    i18n.t("bot.button.bottle.throw_back", locale=user_config.lang),
                    callback_data=f"throw_back {user.id}",
                ),
            ]
            if db_user is not None and db_user.id == bottle.sender_id:
                row1.append(
                    types.InlineKeyboardButton(
                        i18n.t("bot.button.bottle.destroy", locale=user_config.lang),
                        callback_data=f"destroy_bottle {bottle.id} {user.id}",
                    )
                )
            else:
                row1.append(
                    types.InlineKeyboardButton(
                        i18n.t("bot.button.bottle.reply", locale=user_config.lang),
                        callback_data=f"reply_bottle_menu {bottle.id} {user.id}",
                    )
                )
            bottle_markup = types.InlineKeyboardMarkup(
                [
                    row1,
                    [
                        types.InlineKeyboardButton(
                            i18n.t("bot.button.bottle.report", locale=user_config.lang),
                            callback_data=f"report_bottle {bottle.id}",
                        ),
                        types.InlineKeyboardButton(
                            i18n.t("bot.button.bottle.seek", locale=user_config.lang),
                            url=f"https://t.me/{bot_username}?start=seek_bottle_{bottle.id}",
                        ),
                    ],
                ]
            )
            match bottle.media_type:
                case enums.MessageMediaType.PHOTO.name if bottle.file_id:
                    results.append(
                        types.InlineQueryResultCachedPhoto(
                            photo_file_id=bottle.file_id,
                            id=f"pick_bottle_{bottle.id}",
                            title=i18n.t(
                                "bot.inline.pick_bottle_title", locale=user_config.lang
                            ),
                            description=bottle_description,
                            caption=bottle_text,
                            reply_markup=bottle_markup,
                        )
                    )
                case enums.MessageMediaType.VIDEO.name if bottle.file_id:
                    results.append(
                        types.InlineQueryResultCachedVideo(
                            video_file_id=bottle.file_id,
                            title=i18n.t(
                                "bot.inline.pick_bottle_title", locale=user_config.lang
                            ),
                            id=f"pick_bottle_{bottle.id}",
                            description=bottle_description,
                            caption=bottle_text,
                            reply_markup=bottle_markup,
                        )
                    )
                case enums.MessageMediaType.AUDIO.name if bottle.file_id:
                    results.append(
                        types.InlineQueryResultCachedAudio(
                            audio_file_id=bottle.file_id,
                            id=f"pick_bottle_{bottle.id}",
                            caption=bottle_text,
                            reply_markup=bottle_markup,
                        )
                    )
                case enums.MessageMediaType.DOCUMENT.name if bottle.file_id:
                    results.append(
                        types.InlineQueryResultCachedDocument(
                            document_file_id=bottle.file_id,
                            title=i18n.t(
                                "bot.inline.pick_bottle_title", locale=user_config.lang
                            ),
                            id=f"pick_bottle_{bottle.id}",
                            description=bottle_description,
                            caption=bottle_text,
                            reply_markup=bottle_markup,
                        )
                    )
                case enums.MessageMediaType.ANIMATION.name if bottle.file_id:
                    results.append(
                        types.InlineQueryResultCachedAnimation(
                            animation_file_id=bottle.file_id,
                            id=f"pick_bottle_{bottle.id}",
                            title=i18n.t(
                                "bot.inline.pick_bottle_title", locale=user_config.lang
                            ),
                            caption=bottle_text,
                            reply_markup=bottle_markup,
                        )
                    )
                case _:
                    results.append(
                        types.InlineQueryResultArticle(
                            id=f"pick_bottle_{bottle.id}",
                            title=i18n.t(
                                "bot.inline.pick_bottle_title", locale=user_config.lang
                            ),
                            description=bottle_description,
                            input_message_content=types.InputTextMessageContent(
                                message_text=bottle_text
                                or i18n.t(
                                    "bot.msg.bottle.no_bottles",
                                    locale=user_config.lang,
                                ),
                            ),
                            reply_markup=bottle_markup,
                        )
                    )
        else:
            results.append(
                types.InlineQueryResultArticle(
                    id="pick_bottle_empty",
                    title=i18n.t("bot.inline.pick_bottle_title", locale=user_config.lang),
                    description=i18n.t(
                        "bot.msg.bottle.no_bottles", locale=user_config.lang
                    ),
                    input_message_content=types.InputTextMessageContent(
                        message_text=i18n.t(
                            "bot.msg.bottle.no_bottles", locale=user_config.lang
                        ),
                    ),
                )
            )
        if query.chat_type == enums.ChatType.SUPERGROUP:
            results.append(
                types.InlineQueryResultArticle(
                    id="chat_quotes",
                    title=i18n.t(
                        "bot.inline.chat_quotes_title", locale=user_config.lang
                    ),
                    description=i18n.t(
                        "bot.inline.chat_quotes_description", locale=user_config.lang
                    ),
                    input_message_content=types.InputTextMessageContent(
                        message_text=i18n.t(
                            "bot.inline.chat_quotes_quering", locale=user_config.lang
                        ),
                    ),
                    reply_markup=types.InlineKeyboardMarkup(
                        [
                            [
                                types.InlineKeyboardButton(
                                    text=i18n.t(
                                        "bot.inline.chat_quotes_button_noop",
                                        locale=user_config.lang,
                                    ),
                                    callback_data="noop",
                                )
                            ]
                        ]
                    ),
                )
            )
        else:
            results.append(
                types.InlineQueryResultArticle(
                    id="quotes",
                    title=i18n.t("bot.inline.quotes_title", locale=user_config.lang),
                    description=i18n.t(
                        "bot.inline.quotes_description", locale=user_config.lang
                    ),
                    input_message_content=types.InputTextMessageContent(
                        message_text=i18n.t(
                            "bot.inline.quotes_message", locale=user_config.lang
                        ),
                    ),
                    reply_markup=types.InlineKeyboardMarkup(
                        [
                            [
                                types.InlineKeyboardButton(
                                    text=i18n.t(
                                        "bot.inline.quotes_button",
                                        locale=user_config.lang,
                                    ),
                                    switch_inline_query_current_chat="q ",
                                )
                            ]
                        ]
                    ),
                )
            )
        # MEME inline menu disabled by request. Keep the code for future reuse.
        # results.append(
        #     types.InlineQueryResultArticle(
        #         title="Ma Pháp Thiếu Nữ",
        #         description="Tạo meme Ma Pháp Thiếu Nữ: An An nói hoặc tranh biện",
        #         input_message_content=types.InputTextMessageContent(
        #             message_text="""
        # Trình tạo MEME Ma Pháp Thiếu Nữ, cách dùng:
        #
        # 1. An An nói: ms anan [biểu cảm] [nội dung]
        # Ví dụ: ms anan vô_ngữ Giờ ta không muốn nói chuyện
        # 2. Tranh biện: ms trial [nhân vật] ([loại] nội dung...)
        # Ví dụ: ms trial hiro [ngụy chứng] Lúc đó tôi ngủ rất ngon
        # """
        #         ),
        #         reply_markup=types.InlineKeyboardMarkup(
        #             [
        #                 [
        #                     types.InlineKeyboardButton(
        #                         text="An An nói",
        #                         switch_inline_query_current_chat="ms anan Yandere Hãy để mình nói gì đó đi~",
        #                     ),
        #                     types.InlineKeyboardButton(
        #                         text="Tranh biện",
        #                         switch_inline_query_current_chat="ms trial hiro [ngụy chứng] Lúc đó tôi ngủ rất ngon",
        #                     ),
        #                 ]
        #             ]
        #         ),
        #     )
        # )
        await query.answer(
            results=results,
            switch_pm_text=i18n.t("bot.inline.switch_pm_text"),
            switch_pm_parameter="inline_query",
            cache_time=0,
            is_personal=True,
        )
        return
    if datas[0].startswith("q"):
        # quotes
        q_data = datas[0].split("_")
        text = " ".join(datas[1:])
        if len(q_data) > 1:
            try:
                chat_id = int(q_data[1])
            except ValueError:
                chat_id = None
            await query_quote(client, query, chat_id, text)
            return
        await query_quote(client, query, text=text)
    elif datas[0].startswith("ms"):
        # manosaba memes
        datas = datas[1:]
        await handle_manomeme(client, query, datas)


@Client.on_chosen_inline_result()
async def chosen_inline_result(client: Client, result: types.ChosenInlineResult):
    user = result.from_user
    user_config = await database.get_user_config(user)
    info = None
    try:
        info = hack.resolve_inline_message_id(result.inline_message_id)
    except Exception as e:
        logger.warning(f"Failed to resolve inline message id: {e}")
    if info is None:
        await client.edit_inline_text(
            inline_message_id=result.inline_message_id,
            text=i18n.t("bot.inline.resolve_error", locale=user_config.lang),
        )
        return
    if result.result_id == "chat_quotes":
        await client.edit_inline_text(
            inline_message_id=result.inline_message_id,
            text=i18n.t("bot.inline.chat_quotes_success", locale=user_config.lang),
            reply_markup=types.InlineKeyboardMarkup(
                [
                    [
                        types.InlineKeyboardButton(
                            switch_inline_query_current_chat=f"q_{info.chat_id} ",
                            text=i18n.t(
                                "bot.inline.chat_quotes_button",
                                locale=user_config.lang,
                            ),
                        )
                    ]
                ]
            ),
        )
        return
    elif result.result_id == "pick_bottle":
        lang = user_config.lang
        chat = await database.get_chat_by_id(info.chat_id)
        if chat is not None:
            lang = chat.chat_config.lang
            if not chat.chat_config.pick_bottle_enabled:
                await client.edit_inline_text(
                    inline_message_id=result.inline_message_id,
                    text=i18n.t(
                        "bot.msg.bottle.pick_disabled_in_chat",
                        locale=lang,
                    ),
                )
                return
        bottle = await database.pick_random_bottle()
        if bottle is None:
            await client.edit_inline_text(
                inline_message_id=result.inline_message_id,
                text=i18n.t("bot.msg.bottle.no_bottles", locale=user_config.lang),
            )
            return
        bot_username = client.me.username if client.me else None

        row1 = [
            types.InlineKeyboardButton(
                i18n.t("bot.button.bottle.throw_back", locale=lang),
                callback_data=f"throw_back {user.id}",
            )
        ]
        if bottle.sender_id == user.id:
            row1.append(
                types.InlineKeyboardButton(
                    i18n.t("bot.button.bottle.destroy", locale=lang),
                    callback_data=f"destroy_bottle {bottle.id} {user.id}",
                ),
            )
        buttons = [
            [
                types.InlineKeyboardButton(
                    i18n.t("bot.button.bottle.report", locale=lang),
                    callback_data=f"report_bottle {bottle.id}",
                ),
                types.InlineKeyboardButton(
                    i18n.t("bot.button.bottle.seek", locale=lang),
                    url=f"https://t.me/{bot_username}?start=seek_bottle_{bottle.id}",
                ),
            ],
        ]
        if bottle.media_type is not None and bottle.file_id is not None:
            try:
                media = None
                match bottle.media_type:
                    case enums.MessageMediaType.PHOTO.name:
                        media = types.InputMediaPhoto(
                            media=bottle.file_id, caption=bottle.text
                        )
                    case enums.MessageMediaType.VIDEO.name:
                        media = types.InputMediaVideo(
                            media=bottle.file_id, caption=bottle.text
                        )
                    case enums.MessageMediaType.AUDIO.name:
                        media = types.InputMediaAudio(
                            media=bottle.file_id, caption=bottle.text
                        )
                    case enums.MessageMediaType.DOCUMENT.name:
                        media = types.InputMediaDocument(
                            media=bottle.file_id, caption=bottle.text
                        )
                    case enums.MessageMediaType.ANIMATION.name:
                        media = types.InputMediaAnimation(
                            media=bottle.file_id, caption=bottle.text
                        )
                if media is not None:
                    await client.edit_inline_media(
                        inline_message_id=result.inline_message_id,
                        media=media,
                        reply_markup=types.InlineKeyboardMarkup(buttons),
                    )
                    return
            except Exception as e:
                logger.exception(f"Failed to edit inline media: {e}")
        await client.edit_inline_text(
            inline_message_id=result.inline_message_id,
            text=bottle.text,
            reply_markup=types.InlineKeyboardMarkup(buttons),
        )
        return
    elif result.result_id == "seg":
        lang = user_config.lang
        chat = await database.get_chat_by_id(info.chat_id)
        if chat is not None:
            lang = chat.chat_config.lang
            if not chat.chat_config.setu_enabled:
                await client.edit_inline_text(
                    inline_message_id=result.inline_message_id,
                    text=i18n.t("bot.msg.manyacg.chat_setu_disabled", locale=lang),
                )
                return
        if not manyacg_client:
            await client.edit_inline_text(
                inline_message_id=result.inline_message_id,
                text=i18n.t("bot.msg.manyacg.setu_error", locale=lang),
            )
            return
        try:
            resp = await manyacg_client.random_artwork(limit=1, r18=2)
            if resp.status != 200 or not resp.data:
                await client.edit_inline_text(
                    inline_message_id=result.inline_message_id,
                    text=i18n.t("bot.msg.manyacg.setu_error", locale=lang),
                )
                return
            artwork = resp.data[0]
            if not artwork.pictures:
                await client.edit_inline_text(
                    inline_message_id=result.inline_message_id,
                    text=i18n.t("bot.msg.manyacg.setu_error", locale=lang),
                )
                return
            picture = artwork.pictures[random.randint(0, len(artwork.pictures) - 1)]
            await client.edit_inline_media(
                inline_message_id=result.inline_message_id,
                media=types.InputMediaPhoto(
                    media=picture.regular,
                    caption=f"<a href='{artwork.source_url}'>{html.escape(artwork.title)}</a>",
                    parse_mode=enums.ParseMode.HTML,
                    has_spoiler=artwork.r18,
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    [
                        [
                            types.InlineKeyboardButton(
                                text=i18n.t("bot.button.manyacg.detail", locale=lang),
                                url=artwork.source_url,
                            ),
                            types.InlineKeyboardButton(
                                text=i18n.t("bot.button.manyacg.original", locale=lang),
                                url=f"https://t.me/{app_config.manyacg_bot}/?start=file_{picture.id}",
                            ),
                        ]
                    ]
                ),
            )
        except Exception as e:
            logger.error(f"inline seg error: {e.__class__.__name__}:{e}")
            await client.edit_inline_text(
                inline_message_id=result.inline_message_id,
                text=i18n.t("bot.msg.manyacg.setu_error", locale=lang),
            )
        return
    elif result.result_id.startswith("ms_"):
        dataid = result.result_id.split("_")[1]
        data: dict | None = await memttlcache.get(f"manomeme_inline:{dataid}")
        if data is None:
            await client.edit_inline_text(
                inline_message_id=result.inline_message_id,
                text="查询过期了呢, 请重新生成",
            )
            return
        match data["type"]:
            case "anan":
                face = data.get("face", "无语")
                text = data.get("text", "吾辈现在不想说话")
                try:
                    image_bytes = await asyncio.to_thread(
                        manomeme.draw_anan, text, face
                    )
                    media = BytesIO(image_bytes)
                    media.name = "anan.png"
                    await client.edit_inline_media(
                        inline_message_id=result.inline_message_id,
                        media=types.InputMediaPhoto(media=media),
                        reply_markup=types.InlineKeyboardMarkup(
                            [
                                [
                                    types.InlineKeyboardButton(
                                        text="安安说",
                                        switch_inline_query_current_chat=f"ms anan {face} ",
                                    )
                                ]
                            ]
                        ),
                    )
                except Exception as e:
                    logger.exception(f"Failed to edit inline media: {e}")
                    await client.edit_inline_text(
                        inline_message_id=result.inline_message_id,
                        text="生成图片失败了呢, 请稍后再试",
                    )
                return
            case "trial":
                character = data.get("character", manomeme.Character.EMA)
                options = data.get("options", [])
                if not options:
                    await client.edit_inline_text(
                        inline_message_id=result.inline_message_id,
                        text="没有有效的选项呢, 请重新生成",
                    )
                    return
                try:
                    image_bytes = await asyncio.to_thread(
                        manomeme.draw_trial, character, options
                    )
                    media = BytesIO(image_bytes)
                    media.name = "trial.png"
                    await client.edit_inline_media(
                        inline_message_id=result.inline_message_id,
                        media=types.InputMediaPhoto(media=media),
                    )
                except Exception as e:
                    logger.exception(f"Failed to edit inline media: {e}")
                    await client.edit_inline_text(
                        inline_message_id=result.inline_message_id,
                        text="生成图片失败了呢, 请稍后再试",
                    )
                return
