import json

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from waku import i18n

_TITLE_PERMISSION_FALLBACKS = {
    "can_change_info": "Change info",
    "can_manage_tags": "Manage tags",
    "can_delete_messages": "Delete messages",
    "can_edit_messages": "Edit messages",
    "can_restrict_members": "Restrict members",
    "can_invite_users": "Invite users",
    "can_promote_members": "Promote members",
    "can_post_stories": "Post stories",
    "can_edit_stories": "Edit stories",
    "can_delete_stories": "Delete stories",
    "can_manage_video_chats": "Manage video chats",
    "can_manage_topics": "Manage topics",
    "can_pin_messages": "Pin messages",
}


def _permission_label(permission: str, lang: str) -> str:
    key = f"bot.button.title_permissions.{permission}"
    text = i18n.t(key, locale=lang)
    if text == key or text.startswith("bot.button."):
        return _TITLE_PERMISSION_FALLBACKS.get(permission, permission)
    return text


def _permission_button(
    permission: str, enabled: bool, lang: str = "zh-CN"
) -> InlineKeyboardButton:
    status = "✅" if enabled else "❌"
    return InlineKeyboardButton(
        f"{status} {_permission_label(permission, lang)}",
        callback_data=f"set_title_permissions toggle {permission}",
    )


class TitlePermissionsMarkup:
    def __init__(self, permissions: dict[str, bool] = {}, lang: str = "zh-CN") -> None:
        self.lang = lang
        if isinstance(permissions, str):
            permissions = json.loads(permissions)
        self.permissions = permissions

    def build(self) -> InlineKeyboardMarkup:
        permission_groups = [
            ["can_change_info", "can_delete_messages", "can_manage_tags"],
            ["can_restrict_members", "can_invite_users", "can_promote_members"],
            ["can_post_stories", "can_edit_stories", "can_delete_stories"],
            ["can_manage_video_chats", "can_manage_topics", "can_pin_messages"],
        ]

        keyboard = []
        for row in permission_groups:
            keyboard_row = []
            for permission in row:
                enabled = self.permissions.get(permission, False)
                keyboard_row.append(_permission_button(permission, enabled, self.lang))
            keyboard.append(keyboard_row)

        keyboard.append(
            [
                InlineKeyboardButton(
                    i18n.t("bot.button.chat_config.save", locale=self.lang),
                    callback_data="set_title_permissions save",
                )
            ]
        )
        return InlineKeyboardMarkup(keyboard)
