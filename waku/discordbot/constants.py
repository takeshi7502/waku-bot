from __future__ import annotations

import re

DISCORD_MEMBERS_INTENT = True
DISCORD_COMMAND_PREFIX = "!"
DISCORD_MESSAGE_HISTORY_LIMIT = 20
DISCORD_REPLY_MAX_MESSAGES = 7
DISCORD_REPLY_DELAY_MIN = 0.7
DISCORD_REPLY_DELAY_MAX = 3.0
DISCORD_AGENT_MAX_CONCURRENT = 2
DISCORD_AGENT_BUSY_TIMEOUT = 20.0

DISCORD_AUTH_STATUS_NONE = "none"
DISCORD_AUTH_STATUS_PENDING = "pending"
DISCORD_AUTH_STATUS_REJECTED = "rejected"
DISCORD_AUTH_REQUEST_BUTTON_ID = "waku:discord:auth:request"
DISCORD_AUTH_APPROVE_BUTTON_ID = "waku:discord:auth:approve"
DISCORD_AUTH_REJECT_BUTTON_ID = "waku:discord:auth:reject"

_DISCORD_IMAGE_BATCH_MAX = 3

_DISCORD_IMAGE_SEND_DELAY_SECONDS = 3.0

_DISCORD_IMAGE_BUSY_WAIT_SECONDS = 5

_DISCORD_GUILD_SETTINGS_CACHE_PREFIX = "discord_guild_settings:"

_DISCORD_GUILD_SETTINGS_CACHE_TTL = 300

_DISCORD_GROUP_MEMORY_BATCH_SIZE = 100

_DISCORD_GROUP_MEMORY_TTL = 86400 * 7

_DISCORD_BUSY_REPLIES = (
    "Waku đang kẹt nhiều request Discord cùng lúc, chờ mình vài giây rồi gọi lại nha 🫠",
    "Server đang gọi Waku hơi dồn dập, mình cần thở một nhịp rồi xử lý tiếp nha.",
    "Waku hơi quá tải bên Discord rồi, thử gọi lại sau vài giây giúp mình nha.",
)

_DISCORD_TEMPORARY_ERROR_REPLIES = (
    "Waku đang bị nghẽn nhẹ khi xử lý Discord, thử gọi lại sau chút nha.",
    "Đường trả lời của Waku đang hơi quá tải, chờ một nhịp rồi gọi lại mình nha.",
    "Waku chưa xử lý ổn tin này vì bên AI đang bận, thử lại sau vài giây nha.",
)

_DISCORD_SERVER_LIST_RELOAD_ID = "waku:discord_server_list:reload"

_DISCORD_SERVER_MENU_CACHE_KEY = "discord_server_menu_messages"

_URL_RE = re.compile(r"https?://\S+")

_MENTION_RE = re.compile(r"<@!?\d+>")

_KEYWORD_SEPARATORS = (" ", "\n", "\t", ",", ":", "，", "：", "!", "！", "?", "？")

_SETU_COMMANDS = ("setu", "/setu", "!setu", ".setu", "涩图", "色图")

_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]"
)

_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:\d{15,25}>")

_R18_KEYWORDS = (
    "r18",
    "nsfw",
    "hentai",
    "ero",
    "ecchi",
    "nude",
    "naked",
    "porn",
    "sex",
    "lewd",
    "18+",
    "🔞",
)

__all__ = ['DISCORD_MEMBERS_INTENT', 'DISCORD_COMMAND_PREFIX', 'DISCORD_MESSAGE_HISTORY_LIMIT', 'DISCORD_REPLY_MAX_MESSAGES', 'DISCORD_REPLY_DELAY_MIN', 'DISCORD_REPLY_DELAY_MAX', 'DISCORD_AGENT_MAX_CONCURRENT', 'DISCORD_AGENT_BUSY_TIMEOUT', 'DISCORD_AUTH_STATUS_NONE', 'DISCORD_AUTH_STATUS_PENDING', 'DISCORD_AUTH_STATUS_REJECTED', 'DISCORD_AUTH_REQUEST_BUTTON_ID', 'DISCORD_AUTH_APPROVE_BUTTON_ID', 'DISCORD_AUTH_REJECT_BUTTON_ID', '_DISCORD_IMAGE_BATCH_MAX', '_DISCORD_IMAGE_SEND_DELAY_SECONDS', '_DISCORD_IMAGE_BUSY_WAIT_SECONDS', '_DISCORD_GUILD_SETTINGS_CACHE_PREFIX', '_DISCORD_GUILD_SETTINGS_CACHE_TTL', '_DISCORD_GROUP_MEMORY_BATCH_SIZE', '_DISCORD_GROUP_MEMORY_TTL', '_DISCORD_BUSY_REPLIES', '_DISCORD_TEMPORARY_ERROR_REPLIES', '_DISCORD_SERVER_LIST_RELOAD_ID', '_DISCORD_SERVER_MENU_CACHE_KEY', '_URL_RE', '_MENTION_RE', '_KEYWORD_SEPARATORS', '_SETU_COMMANDS', '_EMOJI_RE', '_CUSTOM_EMOJI_RE', '_R18_KEYWORDS']
