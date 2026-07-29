"""Model-to-schema conversion.

Shared so the same database row always renders the same way, whichever router
returns it.
"""

from __future__ import annotations

from datetime import datetime

from waku.config import app_config
from waku.database.models import ChatConfig, ChatData, Quote, UserData
def _u(*codes: int) -> str:
    return "".join(chr(code) for code in codes)


_MOJIBAKE_REPLACEMENTS = {
    _u(0x00C3, 0x00A0): _u(0x00E0),
    _u(0x00C3, 0x00A1): _u(0x00E1),
    _u(0x00C3, 0x00A2): _u(0x00E2),
    _u(0x00C3, 0x00A3): _u(0x00E3),
    _u(0x00C3, 0x00A8): _u(0x00E8),
    _u(0x00C3, 0x00A9): _u(0x00E9),
    _u(0x00C3, 0x00AA): _u(0x00EA),
    _u(0x00C3, 0x00AC): _u(0x00EC),
    _u(0x00C3, 0x00AD): _u(0x00ED),
    _u(0x00C3, 0x00B2): _u(0x00F2),
    _u(0x00C3, 0x00B3): _u(0x00F3),
    _u(0x00C3, 0x00B4): _u(0x00F4),
    _u(0x00C3, 0x00B5): _u(0x00F5),
    _u(0x00C3, 0x00B9): _u(0x00F9),
    _u(0x00C3, 0x00BA): _u(0x00FA),
    _u(0x00C3, 0x00BD): _u(0x00FD),
    _u(0x00C4, 0x2018): _u(0x0111),
    _u(0x00C4, 0x0090): _u(0x0110),
    _u(0x0393, 0x00C7, 0x00AA): _u(0x2026),
    _u(0x0393, 0x00C7, 0x00B4): _u(0x2013),
    _u(0x0393, 0x00C7, 0x00B6): _u(0x2014),
    _u(0x0393, 0x00C7, 0x02DC): _u(0x2018),
    _u(0x0393, 0x00C7, 0x00D6): _u(0x2019),
    _u(0x0393, 0x00C7, 0x0153): _u(0x201C),
    _u(0x0393, 0x00C7, 0x009D): _u(0x201D),
    _u(0x0393, 0x00C7, 0x00B3): _u(0x2022),
    _u(0x0393, 0x00C7, 0x00BA): _u(0x203A),
    _u(0x00C2, 0x00A0): " ",
    _u(0x00C2): "",
}


def clean_mojibake(value: str | None) -> str | None:
    if value is None:
        return None
    for bad, good in _MOJIBAKE_REPLACEMENTS.items():
        value = value.replace(bad, good)
    return value


from waku.webapp.schemas import (
    AdminChatOut,
    AdminUserOut,
    ChatConfigOut,
    QuoteOut,
)


def timestamp(value: datetime | None) -> str:
    """Render a timestamp as ISO 8601, or an empty string when unset.

    The frontend formats dates with `Intl.DateTimeFormat` in the user's locale, so
    the API only has to be unambiguous.
    """
    return value.isoformat() if value is not None else ""


def quote_out(quote: Quote, chat_title: str | None = None) -> QuoteOut:
    # `quote.user` is lazy="noload", so it is only populated when the query asked
    # for it. Reading the attribute directly would raise outside a session.
    user_name: str | None = None
    loaded_user = quote.__dict__.get("user")
    if isinstance(loaded_user, UserData):
        user_name = clean_mojibake(loaded_user.full_name)

    return QuoteOut(
        link=quote.link,
        chat_id=quote.chat_id,
        chat_title=clean_mojibake(chat_title),
        user_id=quote.user_id,
        user_name=user_name,
        message_id=quote.message_id,
        text=clean_mojibake(quote.text),
        has_image=bool(quote.img),
        created_at=timestamp(quote.created_at),
    )


def chat_config_out(config: ChatConfig) -> ChatConfigOut:
    return ChatConfigOut(
        waifu_enabled=config.waifu_enabled,
        delete_events_enabled=config.delete_events_enabled,
        unpin_channel_pin_enabled=config.unpin_channel_pin_enabled,
        message_search_enabled=config.message_search_enabled,
        quote_probability=config.quote_probability,
        quote_pin_message=config.quote_pin_message,
        title_permissions=_normalize_permissions(config.title_permissions),
        greeting=config.greeting,
        ai_reply=config.ai_reply,
        ai_reply_other_bots_enabled=config.ai_reply_other_bots_enabled,
        ai_comment=config.ai_comment,
        setu_enabled=config.setu_enabled,
        convert_b23_enabled=config.convert_b23_enabled,
        parse_artwork_enabled=config.parse_artwork_enabled,
        pick_bottle_enabled=config.pick_bottle_enabled,
        group_memory_enabled=config.group_memory_enabled,
        lang=config.lang,
    )


def _normalize_permissions(raw: dict | str | None) -> dict[str, bool]:
    """Coerce stored title permissions into a plain bool map.

    Some older rows hold a JSON string rather than an object (see the warning in
    plugins/title/title.py), so both shapes have to be tolerated on read.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        raw = parsed if isinstance(parsed, dict) else {}
    return {str(key): bool(value) for key, value in raw.items()}


def admin_chat_out(chat: ChatData, member_count: int) -> AdminChatOut:
    return AdminChatOut(
        id=chat.id,
        title=chat.title,
        username=chat.username,
        member_count=member_count,
        created_at=timestamp(chat.created_at),
    )


def admin_user_out(user: UserData) -> AdminUserOut:
    config = user.user_config
    return AdminUserOut(
        id=user.id,
        full_name=user.full_name,
        username=user.username,
        lang=config.lang,
        coins=config.coins,
        affection=config.affection,
        waifu_mention=user.waifu_mention,
        is_bot=user.is_bot,
        is_real_user=user.is_real_user,
        is_bot_global_admin=user.is_bot_global_admin,
        is_owner=user.id in app_config.owners,
        is_married=user.is_married,
        married_waifu_id=user.married_waifu_id,
        created_at=timestamp(user.created_at),
    )
