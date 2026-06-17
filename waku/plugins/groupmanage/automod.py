import asyncio
import re
from datetime import datetime, UTC, timedelta

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import RPCError
from pyrogram.types import ChatPermissions, Message
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from waku import common, database
from waku.config import app_config
from waku.logger import logger

# Fast filter regexes
# Matches Telegram invite links like t.me/joinchat, t.me/+, telegram.me/joinchat, etc.
_TG_INVITE_RE = re.compile(
    r"(?:t(?:elegram)?\.me|telegram\.dog)/(?:joinchat/|\+)[A-Za-z0-9_-]+",
    re.IGNORECASE
)

# Common crypto/scam/porn keywords
_SPAM_KEYWORDS = [
    "airdrop", "solana", "giveaway", "casino", "lì xì", "nhận quà", 
    "x2 coin", "nhân đôi tài sản", "crypto pump", "xxx porn", "phim sex",
    "gái gọi", "sugar baby", "tuyển ctv", "làm nhiệm vụ", "đầu tư sinh lời",
    "nhiệm vụ nhận tiền", "kiếm tiền online"
]

class ModResult(BaseModel):
    is_spam: bool = Field(description="True if the message is spam, advertisement, scam, or highly toxic/abusive/pornographic.")
    reason: str = Field(description="Brief reason for the moderation decision.")

_mod_agent = None

def _get_mod_agent():
    global _mod_agent
    if _mod_agent is None:
        from waku.plugins.agent.agent import small_model
        _mod_agent = Agent(
            model=small_model,
            result_type=ModResult,
            system_prompt=(
                "You are an expert Telegram chat moderator. Analyze the message content "
                "and determine if it is spam, advertisement, scam, or highly toxic/abusive/pornographic. "
                "Be conservative but accurate. Normal chat, jokes, or swearing without abuse should be classified as safe (is_spam=False)."
            )
        )
    return _mod_agent


async def execute_moderation_action(client: Client, chat_id: int, user_id: int, message_id: int, reason: str):
    logger.info(f"[Auto-Mod] Moderating user {user_id} in chat {chat_id}. Reason: {reason}")
    
    # 1. Delete message
    try:
        await client.delete_messages(chat_id, message_id)
    except RPCError as e:
        logger.warning(f"[Auto-Mod] Failed to delete message {message_id}: {e}")

    # 2. Mute user for 24 hours
    until_date = datetime.now(UTC) + timedelta(hours=24)
    try:
        await client.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(),
            until_date=until_date,
        )
    except RPCError as e:
        logger.warning(f"[Auto-Mod] Failed to mute user {user_id}: {e}")

    # 3. Send warning notification in chat and delete it after 10 seconds
    try:
        warning_msg = await client.send_message(
            chat_id,
            f"⚠️ **[Auto-Mod]** Đã xóa tin nhắn nghi vấn spam và tắt tiếng thành viên 24h.\n"
            f"_Lý do: {reason}_"
        )
        
        async def delete_later():
            await asyncio.sleep(10)
            try:
                await warning_msg.delete()
            except Exception:
                pass
        
        asyncio.create_task(delete_later())
    except Exception as e:
        logger.warning(f"[Auto-Mod] Failed to send warning message: {e}")


async def run_ai_moderation(client: Client, chat_id: int, user_id: int, message_id: int, text: str):
    try:
        mod_agent = _get_mod_agent()
        result = await mod_agent.run(f"Message content: {text}")
        if result.data.is_spam:
            logger.info(f"[Auto-Mod] AI flagged message {message_id} in {chat_id} from {user_id} as spam: {result.data.reason}")
            await execute_moderation_action(client, chat_id, user_id, message_id, f"AI Filter: {result.data.reason}")
    except Exception as e:
        logger.error(f"[Auto-Mod] Error in AI moderation: {e}")


@Client.on_message(filters.group & ~filters.service & ~filters.me, group=5)
async def auto_moderator(client: Client, message: Message):
    if not app_config.agent:
        return

    chat = message.chat
    if not chat or not chat.id:
        return

    # 1. Check if automod is enabled for this chat
    chat_config = await database.get_chat_config(chat.id)
    if not chat_config.agent_automod_enabled:
        return

    user = message.sender_chat or message.from_user
    if not user or not user.id:
        return

    # 2. Skip checks for protected members (admins, owners)
    try:
        member = await common.get_chat_member(client, chat.id, user.id)
        if member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return
    except Exception:
        pass

    text = message.text or message.caption or ""
    if not text:
        return

    # 3. Fast Filter Layer (RegEx & Keywords)
    is_violation = False
    violation_reason = ""

    if _TG_INVITE_RE.search(text):
        is_violation = True
        violation_reason = "Telegram group/channel invite link"
    else:
        text_lower = text.lower()
        for kw in _SPAM_KEYWORDS:
            if kw in text_lower:
                is_violation = True
                violation_reason = f"keyword '{kw}'"
                break

    if is_violation:
        await execute_moderation_action(client, chat.id, user.id, message.id, f"Chứa {violation_reason}")
        return

    # 4. AI Analysis Layer (Semantic/Suspect detection)
    has_link = "http://" in text or "https://" in text or "t.me/" in text
    has_mention = "@" in text
    has_suspect_keywords = any(w in text.lower() for w in ["kiếm tiền", "inbox", "ib", "kênh", "group", "tele", "tuyen", "tuyển"])

    if has_link or has_mention or has_suspect_keywords:
        # Run AI check in background to avoid blocking other message handlers
        asyncio.create_task(run_ai_moderation(client, chat.id, user.id, message.id, text))
