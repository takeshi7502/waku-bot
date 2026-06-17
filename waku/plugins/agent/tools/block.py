import asyncio
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic

import pyrogram
from pydantic_ai import RunContext
from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import RPCError
from pyrogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup

from waku import common, database
from waku.affection import get_affection_rank
from waku.config import app_config
from waku.database.models import UserChatAssociation, UserData
from waku.logger import logger
from waku.plugins.member_ops import (
    acquire_group_member_operation,
    describe_group_member_operation,
    get_group_member_operation,
    release_group_member_operation,
    request_group_member_operation_stop,
)

from .. import datatype, state


def _calculate_block_duration(requested_minutes: int, affection_rank: float) -> int:
    """Calculate effective block duration based on affection rank.

    Formula: effective = requested * max(0.1, 1 - affection_rank^2)

    - affection_rank 0.0 (lowest): factor = 1.0, no reduction
    - affection_rank 0.5: factor = 0.75
    - affection_rank 0.8: factor = 0.36
    - affection_rank 1.0 (highest): factor = 0.1, minimum 10%
    """
    factor = max(0.1, 1.0 - affection_rank**2)
    return max(1, round(requested_minutes * factor))


async def _can_request_moderation_action(
    ctx: RunContext[datatype.ContextDeps],
) -> bool:
    """Check whether the current requester may trigger moderation tools.

    This intentionally returns only a boolean so the model never receives bot-admin
    identities or role details that it could reveal later.
    """
    user_id = ctx.deps.user_id
    chat_id = ctx.deps.chat_id

    try:
        if user_id in app_config.owners:
            return True

        db_user = await database.get_user_by_id(user_id)
        if db_user is not None and db_user.is_bot_global_admin:
            return True

        association = await database.get_association(user_id, chat_id)
        if association is not None and association.is_bot_admin:
            return True

        return await common.can_user_manage_bot_in_chat(user_id, chat_id)
    except Exception as e:
        logger.warning(
            f"Failed to verify moderation requester {user_id} "
            f"in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return False


async def _can_request_true_bot_admin_action(ctx: RunContext[datatype.ContextDeps]) -> bool:
    """Only owners and global bot admins, not promoted per-group bot admins."""
    try:
        if ctx.deps.user_id in app_config.owners:
            return True
        db_user = await database.get_user_by_id(ctx.deps.user_id)
        return bool(db_user is not None and db_user.is_bot_global_admin)
    except Exception as e:
        logger.warning(
            f"Failed to verify true bot admin requester {ctx.deps.user_id}: "
            f"{e.__class__.__name__}: {e}"
        )
        return False


async def _is_protected_bot_admin_or_owner(user_id: int, chat_id: int) -> bool:
    """Return whether the target is a bot owner/admin protected from bot moderation."""
    try:
        if user_id in app_config.owners:
            return True

        db_user = await database.get_user_by_id(user_id)
        if db_user is not None and db_user.is_bot_global_admin:
            return True

        association = await database.get_association(user_id, chat_id)
        return bool(association is not None and association.is_bot_admin)
    except Exception as e:
        logger.warning(
            f"Failed to verify protected target {user_id} in chat {chat_id}: "
            f"{e.__class__.__name__}: {e}"
        )
        return False


_SELF_MODERATION_ACTIONS = {"ban", "kick", "mute"}
_SELF_MANAGEMENT_ACTIONS = _SELF_MODERATION_ACTIONS | {"set tag", "clear tag"}
_MAX_MEMBER_TAG_LENGTH = 16
_GAY_MODE_CALLBACK_PREFIX = "waku_gaymode"
_GAY_MODE_PROGRESS_INTERVAL_SECONDS = 5.0
_GAY_MODE_PROGRESS_BAR_WIDTH = 12


@dataclass
class GayModeJob:
    chat_id: int
    owner_id: int
    mode: str
    total: int
    skipped: int
    operation: object
    task: asyncio.Task | None = None
    progress_message: pyrogram.types.Message | None = None
    stop_requested: bool = False
    processed: int = 0
    ok: int = 0
    errors: int = 0
    started_at: float = 0.0
    last_progress_at: float = 0.0


_GAY_MODE_JOBS: dict[int, GayModeJob] = {}
_SELF_TARGET_ALIASES = {
    "self",
    "me",
    "myself",
    "toi",
    "tôi",
    "tao",
    "t",
    "tui",
    "minh",
    "mình",
    "to",
    "tớ",
    "ban than",
    "bản thân",
    "chính mình",
    "chinh minh",
}


def _is_self_moderation_request(
    ctx: RunContext[datatype.ContextDeps], user_id: int | None, action: str
) -> bool:
    """Return whether this action is a user asking to moderate themselves."""
    return action in _SELF_MODERATION_ACTIONS and user_id == ctx.deps.user_id


def _is_self_management_request(
    ctx: RunContext[datatype.ContextDeps], user_id: int | None, action: str
) -> bool:
    """Return whether this action is a user asking to manage themselves."""
    return action in _SELF_MANAGEMENT_ACTIONS and user_id == ctx.deps.user_id


async def _ensure_group_management_allowed(
    ctx: RunContext[datatype.ContextDeps], action: str, target_user_id: int | None = None
) -> str | None:
    """Return a refusal reason when an AI group-management action is not allowed."""
    chat_id = ctx.deps.chat_id
    if chat_id >= 0:
        return f"Cannot {action} users outside group chats."

    chat_config = await database.get_chat_config(chat_id)
    if not chat_config.agent_group_manage_enabled:
        return "AI group management is disabled for this group. Ask a group manager to enable it in /config first."
    if _is_self_management_request(ctx, target_user_id, action):
        return None
    if not await _can_request_moderation_action(ctx):
        return f"Only group admins or bot admins can ask me to {action} other users."
    return None


async def _get_checked_target_member(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int,
    action: str,
    allow_requester: bool = False,
):
    """Get and validate a moderation target that must be an active non-admin member."""
    chat_id = ctx.deps.chat_id
    me = await ctx.deps.client.get_me()
    if user_id == me.id:
        return None, f"Refusing to {action} myself."
    if user_id == ctx.deps.user_id and not allow_requester:
        return None, f"Refusing to {action} the user currently talking to me."

    try:
        member = await common.get_chat_member(ctx.deps.client, chat_id, user_id)
    except Exception as e:
        logger.warning(f"Failed to check member {user_id} before {action}: {e}")
        return None, f"Cannot verify target membership: {e.__class__.__name__}."

    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return None, f"User {user_id} is not an active member of this group."
    if await _is_protected_bot_admin_or_owner(user_id, chat_id):
        return None, f"Cannot {action} bot admin or owner."
    if member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        return None, f"Refusing to {action} a group owner or administrator."
    return member, None

_USER_ID_RE = re.compile(r"(?<!\d)-?\d{5,}(?!\d)")
_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})|t\.me/([A-Za-z0-9_]{5,32})")
_TG_USER_ID_RE = re.compile(r"tg://user\?id=(-?\d+)")


async def _resolve_user_by_name_in_chat(chat_id: int, target: str) -> tuple[int | None, str | None]:
    """Resolve a group member by username/full name/member tag only when safe."""
    result = await database.resolve_member_by_name(chat_id, target)
    if result.user_id is not None:
        return result.user_id, None
    if result.reason == "ambiguous":
        preview = "; ".join(
            f"{name} (@{username}) id={user_id} tag={tag or '-'}"
            for user_id, name, username, tag in result.matches[:8]
        )
        return None, f"Multiple users match that name/tag. Ask the user to clarify: {preview}"
    return None, "Cannot resolve target user from stored data. Ask them to reply, mention @username, provide user ID, or run /syncmembers first."


async def _resolve_moderation_target(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None,
    target: str,
) -> tuple[int | None, str | None]:
    """Resolve moderation target without requiring the model to know a numeric ID."""
    target = target.strip()
    if user_id is not None:
        return user_id, None

    normalized_target = target.casefold().strip(" \t\r\n\"'`.,!?;:()[]{}")
    if normalized_target in _SELF_TARGET_ALIASES:
        return ctx.deps.user_id, None

    if match := _TG_USER_ID_RE.search(target):
        return int(match.group(1)), None
    if match := _USER_ID_RE.search(target):
        return int(match.group(0)), None

    if match := _USERNAME_RE.search(target):
        username = match.group(1) or match.group(2)
        try:
            user = await ctx.deps.client.get_users(username)
        except RPCError as e:
            logger.warning(f"Failed to resolve username @{username}: {e}")
            return None, f"Cannot resolve @{username}: {e.__class__.__name__}."
        if isinstance(user, list):
            user = user[0] if user else None
        if user and user.id:
            return user.id, None
        return None, f"Cannot resolve @{username}."

    if common.is_explicit_reply(ctx.deps.message) and ctx.deps.message.reply_to_message:
        reply = ctx.deps.message.reply_to_message
        reply_sender = reply.sender_chat or reply.from_user
        if reply_sender and reply_sender.id:
            return reply_sender.id, None

    if target:
        return await _resolve_user_by_name_in_chat(ctx.deps.chat_id, target)

    return None, "Cannot resolve target user. Reply to their message, mention @username, or provide user ID."


async def _resolve_checked_target_member(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None,
    target: str,
    action: str,
):
    resolved_user_id, refusal = await _resolve_moderation_target(ctx, user_id, target)
    if refusal or resolved_user_id is None:
        return None, None, refusal
    if refusal := await _ensure_group_management_allowed(ctx, action, resolved_user_id):
        return resolved_user_id, None, refusal
    member, refusal = await _get_checked_target_member(
        ctx,
        resolved_user_id,
        action,
        allow_requester=_is_self_moderation_request(ctx, resolved_user_id, action),
    )
    return resolved_user_id, member, refusal


def _unmute_permissions() -> ChatPermissions:
    """Common send permissions used to remove a user's mute restriction."""
    return ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_send_polls=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
    )


async def block_user(
    ctx: RunContext[datatype.ContextDeps],
    duration_minutes: int,
    user_id: int | None = None,
    reason: str = "",
) -> str:
    """Block a user from triggering you for a specified duration.

    The user will not be able to trigger any response from you until the block expires.

    Args:
        duration_minutes: Requested block duration in minutes (1~10080, i.e. 1 minute to 7 days).
        user_id: The Telegram user ID to block. If not provided, defaults to the current user.
        reason: Brief reason for blocking this user (optional).

    Returns:
        A description of the block result including the actual duration.
    """
    duration_minutes = max(1, min(10080, duration_minutes))

    target_id = user_id if user_id is not None else ctx.deps.user_id

    is_group = ctx.deps.chat_id < -100
    if is_group and user_id is not None:
        try:
            member = await common.get_chat_member(
                ctx.deps.client, ctx.deps.chat_id, user_id
            )
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return f"User {user_id} is not a member of this group, cannot block."
        except Exception as e:
            logger.warning(f"Failed to check membership for user {user_id}: {e}")
            return f"Cannot verify if user {user_id} is in this group: {e.__class__.__name__}"

    if await _is_protected_bot_admin_or_owner(target_id, ctx.deps.chat_id):
        return f"Cannot block bot admin or owner {target_id}."

    if await common.memttlcache.get(state.user_block_immune_key(target_id)):
        return f"User {target_id} is currently immune to being blocked."

    affection_rank = await get_affection_rank(target_id)
    effective_minutes = _calculate_block_duration(duration_minutes, affection_rank)
    ttl_seconds = effective_minutes * 60

    await common.memttlcache.set(
        state.user_blocked_key(target_id), True, ttl=ttl_seconds
    )

    logger.info(
        f"User {target_id} blocked by agent for {effective_minutes} minutes "
        f"(requested: {duration_minutes}, affection_rank: {affection_rank:.4f}, "
        f"reason: {reason!r})"
    )

    return (
        f"User {target_id} has been blocked for {effective_minutes} minutes "
        f"(requested {duration_minutes} min, reduced by affection rank)."
    )


async def ban_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    reason: str = "",
) -> str:
    """Ban a Telegram user from the current group.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or "me" for a user's self-ban request. If omitted while replying to a user's message, the replied user is used.
        reason: Brief reason for banning this user. Do not guess the target when unsure.
    """
    user_id, _, refusal = await _resolve_checked_target_member(ctx, user_id, target, "ban")
    if refusal or user_id is None:
        return refusal

    chat_id = ctx.deps.chat_id
    try:
        await ctx.deps.client.ban_chat_member(chat_id, user_id)
    except RPCError as e:
        logger.warning(
            f"Failed to ban user {user_id} in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return f"Failed to ban user {user_id}: {e.__class__.__name__}. The bot may lack ban permissions."

    logger.warning(
        f"Agent banned user {user_id} in chat {chat_id}; requested by {ctx.deps.user_id}; reason: {reason!r}"
    )
    return f"User {user_id} has been banned from this group."


async def kick_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    reason: str = "",
) -> str:
    """Kick a Telegram user from the current group while allowing them to rejoin.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or "me" for a user's self-kick request. If omitted while replying to a user's message, the replied user is used.
        reason: Brief reason for kicking this user. Do not guess the target when unsure.
    """
    user_id, _, refusal = await _resolve_checked_target_member(ctx, user_id, target, "kick")
    if refusal or user_id is None:
        return refusal

    chat_id = ctx.deps.chat_id
    try:
        await ctx.deps.client.ban_chat_member(chat_id, user_id)
        await ctx.deps.client.unban_chat_member(chat_id, user_id)
    except RPCError as e:
        logger.warning(
            f"Failed to kick user {user_id} in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return f"Failed to kick user {user_id}: {e.__class__.__name__}. The bot may lack ban permissions."

    logger.warning(
        f"Agent kicked user {user_id} in chat {chat_id}; requested by {ctx.deps.user_id}; reason: {reason!r}"
    )
    return f"User {user_id} has been kicked from this group and can rejoin."


async def mute_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    duration_minutes: int = 10,
    reason: str = "",
) -> str:
    """Mute a Telegram user in the current group for a limited duration.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or "me" for a user's self-mute request. If omitted while replying to a user's message, the replied user is used.
        duration_minutes: Mute duration in minutes.
        reason: Brief reason for muting this user. Do not guess the target when unsure.
    """
    user_id, _, refusal = await _resolve_checked_target_member(ctx, user_id, target, "mute")
    if refusal or user_id is None:
        return refusal

    duration_minutes = max(1, min(10080, duration_minutes))
    until_date = datetime.now(UTC) + timedelta(minutes=duration_minutes)
    chat_id = ctx.deps.chat_id
    try:
        await ctx.deps.client.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(),
            until_date=until_date,
        )
    except RPCError as e:
        logger.warning(
            f"Failed to mute user {user_id} in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return f"Failed to mute user {user_id}: {e.__class__.__name__}. The bot may lack restrict permissions."

    logger.warning(
        f"Agent muted user {user_id} in chat {chat_id} for {duration_minutes} minutes; "
        f"requested by {ctx.deps.user_id}; reason: {reason!r}"
    )
    return f"User {user_id} has been muted for {duration_minutes} minutes."


async def unban_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    reason: str = "",
) -> str:
    """Unban a Telegram user from the current group.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or reply target.
        reason: Brief reason for unbanning this user. Do not guess the target when unsure.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "unban"):
        return refusal

    user_id, refusal = await _resolve_moderation_target(ctx, user_id, target)
    if refusal or user_id is None:
        return refusal or "Cannot resolve target user. Reply to their message, mention @username, or provide user ID."

    chat_id = ctx.deps.chat_id
    me = await ctx.deps.client.get_me()
    if user_id in (ctx.deps.user_id, me.id):
        return "Refusing to unban myself or the user currently talking to me."

    try:
        await ctx.deps.client.unban_chat_member(chat_id, user_id)
    except RPCError as e:
        logger.warning(
            f"Failed to unban user {user_id} in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return f"Failed to unban user {user_id}: {e.__class__.__name__}. The bot may lack ban permissions."

    logger.warning(
        f"Agent unbanned user {user_id} in chat {chat_id}; requested by {ctx.deps.user_id}; reason: {reason!r}"
    )
    return f"User {user_id} has been unbanned from this group."


async def unmute_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    reason: str = "",
) -> str:
    """Remove a Telegram user's mute restriction in the current group.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, or display name. If omitted while replying to a user's message, the replied user is used.
        reason: Brief reason for unmuting this user. Do not guess the target when unsure.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "unmute"):
        return refusal
    user_id, _, refusal = await _resolve_checked_target_member(ctx, user_id, target, "unmute")
    if refusal or user_id is None:
        return refusal

    chat_id = ctx.deps.chat_id
    try:
        await ctx.deps.client.restrict_chat_member(
            chat_id,
            user_id,
            permissions=_unmute_permissions(),
        )
    except RPCError as e:
        logger.warning(
            f"Failed to unmute user {user_id} in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return f"Failed to unmute user {user_id}: {e.__class__.__name__}. The bot may lack restrict permissions."

    logger.warning(
        f"Agent unmuted user {user_id} in chat {chat_id}; requested by {ctx.deps.user_id}; reason: {reason!r}"
    )
    return f"User {user_id} has been unmuted in this group."


def _validate_member_tag(tag: str) -> str | None:
    """Return an error message when a Telegram member tag is invalid."""
    if not tag or not tag.strip():
        return "Member tag cannot be empty."
    if "\n" in tag or "\r" in tag:
        return "Member tag must be a single line."
    if len(tag) > _MAX_MEMBER_TAG_LENGTH:
        return f"Member tag is too long. Use {_MAX_MEMBER_TAG_LENGTH} characters or fewer."
    return None


async def _resolve_tag_target(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None,
    target: str,
    action: str,
):
    """Resolve and authorize a member tag target."""
    if user_id is None and not target.strip():
        user_id = ctx.deps.user_id

    resolved_user_id, refusal = await _resolve_moderation_target(ctx, user_id, target)
    if refusal or resolved_user_id is None:
        return None, None, refusal

    if refusal := await _ensure_group_management_allowed(ctx, action, resolved_user_id):
        return resolved_user_id, None, refusal

    allow_requester = _is_self_management_request(ctx, resolved_user_id, action)
    member, refusal = await _get_checked_target_member(
        ctx,
        resolved_user_id,
        action,
        allow_requester=allow_requester,
    )
    return resolved_user_id, member, refusal


async def set_member_tag(
    ctx: RunContext[datatype.ContextDeps],
    tag: str,
    user_id: int | None = None,
    target: str = "",
) -> str:
    """Set or change a Telegram member tag/custom title in the current group.

    Args:
        tag: The new member tag. Keep it short, single-line, and at most 16 characters.
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, reply target, or "me". If omitted, the requester is used.

    Normal users may only change their own tag. Group admins and bot admins may
    change other users' tags. The bot must have Telegram's can_manage_tags
    permission ("Sửa thẻ thành viên").
    """
    tag = tag.strip()
    if refusal := _validate_member_tag(tag):
        return refusal

    user_id, _, refusal = await _resolve_tag_target(ctx, user_id, target, "set tag")
    if refusal or user_id is None:
        return refusal

    chat_id = ctx.deps.chat_id
    try:
        await ctx.deps.client.set_chat_member_tag(chat_id, user_id, tag=tag)
    except RPCError as e:
        logger.warning(
            f"Failed to set member tag for user {user_id} in chat {chat_id}: "
            f"{e.__class__.__name__}: {e}"
        )
        return (
            f"Failed to set member tag for user {user_id}: {e.__class__.__name__}. "
            "The bot may lack the Sửa thẻ thành viên / can_manage_tags permission."
        )

    logger.warning(
        f"Agent set member tag for user {user_id} in chat {chat_id}; "
        f"requested by {ctx.deps.user_id}; tag: {tag!r}"
    )
    return f"Member tag for user {user_id} has been set to {tag!r}."


async def clear_member_tag(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
) -> str:
    """Clear a Telegram member tag/custom title in the current group.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, reply target, or "me". If omitted, the requester is used.

    Normal users may only clear their own tag. Group admins and bot admins may
    clear other users' tags. The bot must have Telegram's can_manage_tags
    permission ("Sửa thẻ thành viên").
    """
    user_id, _, refusal = await _resolve_tag_target(ctx, user_id, target, "clear tag")
    if refusal or user_id is None:
        return refusal

    chat_id = ctx.deps.chat_id
    try:
        await ctx.deps.client.set_chat_member_tag(chat_id, user_id, tag=None)
    except RPCError as e:
        logger.warning(
            f"Failed to clear member tag for user {user_id} in chat {chat_id}: "
            f"{e.__class__.__name__}: {e}"
        )
        return (
            f"Failed to clear member tag for user {user_id}: {e.__class__.__name__}. "
            "The bot may lack the Sửa thẻ thành viên / can_manage_tags permission."
        )

    logger.warning(
        f"Agent cleared member tag for user {user_id} in chat {chat_id}; "
        f"requested by {ctx.deps.user_id}"
    )
    return f"Member tag for user {user_id} has been cleared."


async def _ensure_admin_database_access(ctx: RunContext[datatype.ContextDeps], action: str) -> str | None:
    if ctx.deps.chat_id >= 0:
        return f"Cannot {action} outside group chats."
    if not await _can_request_moderation_action(ctx):
        return f"Only bot admins or group managers can {action}."
    return None


async def _ensure_true_bot_admin_access(ctx: RunContext[datatype.ContextDeps], action: str) -> str | None:
    if ctx.deps.chat_id >= 0:
        return f"Cannot {action} outside group chats."
    if not await _can_request_true_bot_admin_action(ctx):
        return f"Only real bot admins can {action}."
    return None


def _format_member_line(assoc: UserChatAssociation, user: UserData) -> str:
    username = f"@{user.username}" if user.username else "no username"
    role = assoc.member_status or ("admin" if assoc.member_is_admin else "member")
    tag = assoc.member_tag or "-"
    return f"{user.full_name} ({username}) id={user.id} role={role} tag={tag}"


async def list_group_members(ctx: RunContext[datatype.ContextDeps], limit: int = 50) -> str:
    """List stored group members from the bot database. Bot admins only."""
    if refusal := await _ensure_admin_database_access(ctx, "list stored group members"):
        return refusal
    limit = max(1, min(limit, 100))
    rows = await database.get_chat_member_snapshots(ctx.deps.chat_id)
    lines = [_format_member_line(assoc, user) for assoc, user in rows[:limit]]
    more = max(0, len(rows) - len(lines))
    suffix = f"\n...and {more} more stored members." if more else ""
    return f"Stored members: {len(rows)}\n" + "\n".join(lines) + suffix


async def get_group_member_info(ctx: RunContext[datatype.ContextDeps], target: str) -> str:
    """Get stored details for one group member by name, username, tag, or ID. Bot admins only."""
    if refusal := await _ensure_admin_database_access(ctx, "read stored member info"):
        return refusal
    user_id, refusal = await _resolve_moderation_target(ctx, None, target)
    if refusal or user_id is None:
        return refusal or "Cannot resolve member."
    rows = await database.get_chat_member_snapshots(ctx.deps.chat_id, include_bots=True)
    for assoc, user in rows:
        if user.id == user_id:
            return _format_member_line(assoc, user)
    return "Member is not stored for this group. Run /syncmembers first."


async def get_or_create_private_invite_link(ctx: RunContext[datatype.ContextDeps]) -> str:
    """Return the public group link, or create/export an invite link for private groups. Bot admins only."""
    if refusal := await _ensure_admin_database_access(ctx, "get group invite links"):
        return refusal
    try:
        chat = await ctx.deps.client.get_chat(ctx.deps.chat_id)
        username = getattr(chat, "username", None)
        if username:
            return f"Group link: https://t.me/{username}"
    except RPCError as e:
        logger.warning(f"Failed to check public group username before invite link: {e}")

    try:
        link_obj = await ctx.deps.client.create_chat_invite_link(ctx.deps.chat_id)
        return f"Invite link: {link_obj.invite_link}"
    except Exception as first_error:
        try:
            link = await ctx.deps.client.export_chat_invite_link(ctx.deps.chat_id)
            return f"Invite link: {link}"
        except RPCError as e:
            logger.warning(f"Failed to create invite link: {first_error}; fallback: {e}")
            return f"Failed to create invite link: {e.__class__.__name__}. The bot may lack invite permissions."


async def _gay_mode_targets(ctx: RunContext[datatype.ContextDeps]):
    rows = await database.get_chat_member_snapshots(ctx.deps.chat_id)
    targets = []
    skipped = 0
    me = await ctx.deps.client.get_me()
    for assoc, user in rows:
        if user.id <= 0 or not user.is_real_user:
            skipped += 1
            continue
        if user.id == me.id or user.is_bot:
            skipped += 1
            continue
        if assoc.member_status in {"owner", "administrator"} or assoc.member_is_admin:
            skipped += 1
            continue
        targets.append((assoc, user))
    return targets, skipped, len(rows)


async def preview_gay_mode(ctx: RunContext[datatype.ContextDeps]) -> str:
    """Preview gay mode targets. Bot admins only; ask for confirmation before activating."""
    if refusal := await _ensure_true_bot_admin_access(ctx, "preview gay mode"):
        return refusal
    targets, skipped, total = await _gay_mode_targets(ctx)
    return (
        f"Gay mode check: total={total}, affected={len(targets)}, ignored={skipped}. "
        "Ask the admin to confirm before calling activate_gay_mode(confirm=True)."
    )


async def stop_syncmembers(ctx: RunContext[datatype.ContextDeps]) -> str:
    """Stop a running /syncmembers job in this group immediately. Bot admins only; no confirmation needed."""
    if refusal := await _ensure_true_bot_admin_access(ctx, "stop syncmembers"):
        return refusal
    operation = request_group_member_operation_stop(ctx.deps.chat_id, "syncmembers")
    if operation is None:
        running_op = get_group_member_operation(ctx.deps.chat_id)
        if running_op is None:
            return "No /syncmembers job is running in this group."
        return f"Cannot stop /syncmembers because {describe_group_member_operation(running_op)} is running instead."
    return "Stop request sent to /syncmembers. It will stop at the next safe checkpoint without confirmation."



def _has_running_gay_mode_job(chat_id: int) -> bool:
    job = _GAY_MODE_JOBS.get(chat_id)
    return bool(job is not None and job.task is not None and not job.task.done())


def _gay_mode_bar(processed: int, total: int) -> str:
    if total <= 0:
        return ""
    filled = min(_GAY_MODE_PROGRESS_BAR_WIDTH, round(processed / total * _GAY_MODE_PROGRESS_BAR_WIDTH))
    return "█" * filled + "░" * (_GAY_MODE_PROGRESS_BAR_WIDTH - filled)


def _elapsed_text(started_at: float) -> str:
    elapsed = max(0, int(monotonic() - started_at))
    minutes, seconds = divmod(elapsed, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _gay_mode_progress_text(job: GayModeJob, status: str) -> str:
    title = "Gaymode" if job.mode == "activate" else "Hủy gaymode"
    return "\n".join([
        f"<b>{title} đang chạy...</b>",
        f"<code>[{_gay_mode_bar(job.processed, job.total)}]</code> {job.processed}/{job.total}",
        f"OK: <code>{job.ok}</code> | Skipped: <code>{job.skipped}</code> | Error: <code>{job.errors}</code>",
        f"Elapsed: <code>{_elapsed_text(job.started_at)}</code>",
        f"Group: <code>{job.chat_id}</code>",
        f"Status: <code>{status}</code>",
    ])


def _gay_mode_stop_markup(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data=f"{_GAY_MODE_CALLBACK_PREFIX}|stop|{chat_id}")]])


def _gay_mode_confirm_markup(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅", callback_data=f"{_GAY_MODE_CALLBACK_PREFIX}|confirm_stop|{chat_id}"),
        InlineKeyboardButton("❌", callback_data=f"{_GAY_MODE_CALLBACK_PREFIX}|cancel_stop|{chat_id}"),
    ]])


async def _edit_gay_mode_progress(job: GayModeJob, status: str, force: bool = False, final: bool = False) -> None:
    now = monotonic()
    if not force and now - job.last_progress_at < _GAY_MODE_PROGRESS_INTERVAL_SECONDS:
        return
    job.last_progress_at = now
    markup = None if final else _gay_mode_stop_markup(job.chat_id)
    try:
        if job.progress_message is None:
            return
        await job.progress_message.edit_text(_gay_mode_progress_text(job, status), reply_markup=markup)
    except Exception as e:
        logger.debug(f"Failed to update gaymode progress for {job.chat_id}: {e}")


async def _send_gay_mode_private_start(client, job: GayModeJob) -> None:
    try:
        job.progress_message = await client.send_message(
            chat_id=job.owner_id,
            text=_gay_mode_progress_text(job, "starting"),
            reply_markup=_gay_mode_stop_markup(job.chat_id),
        )
    except Exception as e:
        logger.warning(f"Failed to send private gaymode progress to {job.owner_id}: {e}")


async def _run_activate_gay_mode_job(
    client,
    job: GayModeJob,
    targets: list[tuple[UserChatAssociation, UserData]],
) -> None:
    await _send_gay_mode_private_start(client, job)
    try:
        for assoc, user in targets:
            if job.stop_requested:
                await _edit_gay_mode_progress(job, "stopped by admin", force=True, final=True)
                return
            tag = f"gay {random.randint(0, 100)}%"
            try:
                await client.set_chat_member_tag(job.chat_id, user.id, tag=tag)
                await database.mark_gay_mode_tag(job.chat_id, user.id, assoc.member_tag)
                job.ok += 1
                job.processed += 1
                await _edit_gay_mode_progress(job, f"setting {user.id}")
            except Exception as e:
                job.errors += 1
                logger.warning(f"Gay mode stopped while setting tag for {user.id}: {e}")
                await _edit_gay_mode_progress(job, f"error at {user.id}: {e.__class__.__name__}", force=True, final=True)
                return
        await _edit_gay_mode_progress(job, "completed", force=True, final=True)
        logger.info(f"Gay mode activation completed in chat {job.chat_id}: ok={job.ok}, skipped={job.skipped}, total={job.total}")
    finally:
        release_group_member_operation(job.chat_id, job.operation)


async def _run_deactivate_gay_mode_job(
    client,
    job: GayModeJob,
    rows: list[tuple[UserChatAssociation, UserData]],
) -> None:
    await _send_gay_mode_private_start(client, job)
    try:
        for assoc, user in rows:
            if job.stop_requested:
                await _edit_gay_mode_progress(job, "stopped by admin", force=True, final=True)
                return
            try:
                await client.set_chat_member_tag(job.chat_id, user.id, tag=assoc.gay_mode_previous_tag)
                await database.clear_gay_mode_state(job.chat_id, user.id, assoc.gay_mode_previous_tag)
                job.ok += 1
                job.processed += 1
                await _edit_gay_mode_progress(job, f"restoring {user.id}")
            except Exception as e:
                job.errors += 1
                logger.warning(f"Gay mode restore stopped for {user.id}: {e}")
                await _edit_gay_mode_progress(job, f"error at {user.id}: {e.__class__.__name__}", force=True, final=True)
                return
        await _edit_gay_mode_progress(job, "completed", force=True, final=True)
        logger.info(f"Gay mode restore completed in chat {job.chat_id}: restored={job.ok}")
    finally:
        release_group_member_operation(job.chat_id, job.operation)


def _cleanup_gay_mode_job(chat_id: int, task: asyncio.Task) -> None:
    job = _GAY_MODE_JOBS.get(chat_id)
    if job is not None and job.task is task:
        _GAY_MODE_JOBS.pop(chat_id, None)


async def activate_gay_mode(ctx: RunContext[datatype.ContextDeps], confirm: bool = False) -> str:
    """Start a backend job to apply gay mode. Requires explicit confirmation."""
    if refusal := await _ensure_true_bot_admin_access(ctx, "activate gay mode"):
        return refusal
    if not confirm:
        return await preview_gay_mode(ctx)
    chat_id = ctx.deps.chat_id
    running_op = get_group_member_operation(chat_id)
    if running_op is not None:
        return f"Cannot start gaymode because {describe_group_member_operation(running_op)} is running for this group."
    if _has_running_gay_mode_job(chat_id):
        return "Gay mode backend is already running for this group. Wait for the final result."
    targets, skipped, total = await _gay_mode_targets(ctx)
    operation = acquire_group_member_operation(chat_id, "gaymode_activate", ctx.deps.user_id)
    if operation is None:
        return "Another member operation is already running for this group."
    job = GayModeJob(chat_id=chat_id, owner_id=ctx.deps.user_id, mode="activate", total=len(targets), skipped=skipped, operation=operation, started_at=monotonic())
    task = asyncio.create_task(_run_activate_gay_mode_job(ctx.deps.client, job, targets))
    job.task = task
    _GAY_MODE_JOBS[chat_id] = job
    task.add_done_callback(lambda done_task: _cleanup_gay_mode_job(chat_id, done_task))
    return "Gay mode backend started. Progress and final result will be sent in private chat."


async def deactivate_gay_mode(ctx: RunContext[datatype.ContextDeps], confirm: bool = False) -> str:
    """Start a backend job to restore member tags changed by gay mode."""
    if refusal := await _ensure_true_bot_admin_access(ctx, "deactivate gay mode"):
        return refusal
    chat_id = ctx.deps.chat_id
    rows = await database.get_gay_mode_applied_members(chat_id)
    if not confirm:
        return f"Gay mode restore check: affected={len(rows)}. Ask for confirmation first."
    running_op = get_group_member_operation(chat_id)
    if running_op is not None:
        return f"Cannot start gaymode restore because {describe_group_member_operation(running_op)} is running for this group."
    if _has_running_gay_mode_job(chat_id):
        return "Gay mode backend is already running for this group. Wait for the final result."
    operation = acquire_group_member_operation(chat_id, "gaymode_deactivate", ctx.deps.user_id)
    if operation is None:
        return "Another member operation is already running for this group."
    job = GayModeJob(chat_id=chat_id, owner_id=ctx.deps.user_id, mode="deactivate", total=len(rows), skipped=0, operation=operation, started_at=monotonic())
    task = asyncio.create_task(_run_deactivate_gay_mode_job(ctx.deps.client, job, rows))
    job.task = task
    _GAY_MODE_JOBS[chat_id] = job
    task.add_done_callback(lambda done_task: _cleanup_gay_mode_job(chat_id, done_task))
    return "Gay mode restore backend started. Progress and final result will be sent in private chat."


@pyrogram.Client.on_callback_query(filters.regex(rf"^{_GAY_MODE_CALLBACK_PREFIX}\|"), group=0)
async def gay_mode_progress_callback(client: pyrogram.Client, query: pyrogram.types.CallbackQuery):
    if query.message is None or query.data is None or query.from_user is None:
        return
    _, action, chat_id_text = query.data.split("|", 2)
    chat_id = int(chat_id_text)
    job = _GAY_MODE_JOBS.get(chat_id)
    if job is None:
        await query.answer("Gaymode task không còn chạy.", show_alert=True)
        return
    if query.from_user.id != job.owner_id:
        await query.answer("Nút này không dành cho bạn.", show_alert=True)
        return
    if action == "stop":
        await query.answer()
        await query.message.edit_text("<b>Dừng gaymode?</b>", reply_markup=_gay_mode_confirm_markup(chat_id))
        return
    if action == "cancel_stop":
        await query.answer("Tiếp tục chạy.")
        job.progress_message = query.message
        await _edit_gay_mode_progress(job, "running", force=True)
        return
    if action == "confirm_stop":
        job.stop_requested = True
        await query.answer("Đang dừng...")
        await query.message.edit_text(_gay_mode_progress_text(job, "stopping..."))
        job.progress_message = query.message



async def delete_messages(
    ctx: RunContext[datatype.ContextDeps],
    message_ids: list[int] | int | None = None,
    target: str = "",
    count: int = 1,
) -> str:
    """Delete messages in the current group.

    Args:
        message_ids: Specific message ID or list of message IDs to delete.
        target: Optional @username, tg://user link, numeric ID, display name, or "me" to delete messages from. If omitted and reply message exists, deletes the replied message.
        count: Number of recent messages to delete when target is specified or when deleting general recent messages (default 1).
    """
    if refusal := await _ensure_group_management_allowed(ctx, "delete messages"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    to_delete: list[int] = []

    # Case 1: Specific message IDs provided
    if message_ids is not None:
        if isinstance(message_ids, int):
            to_delete.append(message_ids)
        elif isinstance(message_ids, list):
            to_delete.extend(message_ids)

    # Case 2: Reply message exists and no target/ids are specified
    elif not target and ctx.deps.message.reply_to_message:
        to_delete.append(ctx.deps.message.reply_to_message.id)

    # Case 3: Target specified - delete recent messages from a target user
    elif target:
        target_user_id, refusal = await _resolve_moderation_target(ctx, None, target)
        if refusal or target_user_id is None:
            return refusal or "Could not resolve target user for deleting messages."

        # Fetch recent messages and filter by sender
        limit = max(1, min(100, count * 5))
        async for msg in client.get_chat_history(chat_id, limit=limit):
            if msg.from_user and msg.from_user.id == target_user_id:
                to_delete.append(msg.id)
            if len(to_delete) >= count:
                break
    # Case 4: No message_ids, target or reply - delete N recent messages in general
    else:
        async for msg in client.get_chat_history(chat_id, limit=count + 1):
            if msg.id != ctx.deps.message.id:
                to_delete.append(msg.id)
            if len(to_delete) >= count:
                break

    if not to_delete:
        return "No messages found to delete."

    try:
        await client.delete_messages(chat_id, to_delete)
        # Also attempt to delete the command message itself to clean up
        try:
            await client.delete_messages(chat_id, ctx.deps.message.id)
        except RPCError:
            pass
        return f"Successfully deleted {len(to_delete)} messages."
    except RPCError as e:
        logger.warning(f"Failed to delete messages in chat {chat_id}: {e}")
        return f"Failed to delete messages: {e.__class__.__name__}. The bot may lack delete permissions."


async def promote_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    title: str = "",
    reason: str = "",
) -> str:
    """Promote a group member to administrator.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or reply target.
        title: Custom administrator title (custom tag/role). Max 16 characters.
        reason: Brief reason for promoting this user.
    """
    user_id, _, refusal = await _resolve_checked_target_member(ctx, user_id, target, "promote")
    if refusal or user_id is None:
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client
    
    try:
        await client.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            privileges=pyrogram.types.ChatAdministratorRights(
                can_manage_chat=True,
                can_change_info=True,
                can_delete_messages=True,
                can_restrict_members=True,
                can_invite_users=True,
                can_pin_messages=True,
                can_manage_video_chats=True,
                can_manage_topics=True,
            ),
        )
        if title:
            # Clean/validate title
            title = title.strip()[:16]
            try:
                await client.set_administrator_title(chat_id, user_id, title)
            except RPCError as e:
                logger.warning(f"Failed to set admin title for user {user_id}: {e}")
    except RPCError as e:
        logger.warning(
            f"Failed to promote user {user_id} in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return f"Failed to promote user {user_id}: {e.__class__.__name__}. The bot may lack promote permissions."

    logger.warning(
        f"Agent promoted user {user_id} in chat {chat_id}; requested by {ctx.deps.user_id}; reason: {reason!r}"
    )
    return f"User {user_id} has been promoted to administrator."


async def demote_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    reason: str = "",
) -> str:
    """Demote a group administrator back to a regular member.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or reply target.
        reason: Brief reason for demoting this user.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "demote"):
        return refusal

    user_id, refusal = await _resolve_moderation_target(ctx, user_id, target)
    if refusal or user_id is None:
        return refusal or "Cannot resolve target user. Reply to their message, mention @username, or provide user ID."

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client
    me = await client.get_me()

    if user_id == me.id:
        return "Refusing to demote myself."
    if user_id == ctx.deps.user_id:
        return "Refusing to demote the user currently talking to me."

    try:
        member = await common.get_chat_member(client, chat_id, user_id)
    except Exception as e:
        logger.warning(f"Failed to check member {user_id} before demote: {e}")
        return f"Cannot verify target membership: {e.__class__.__name__}."

    if member.status == ChatMemberStatus.OWNER:
        return "Refusing to demote the group owner."
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return "User is not an administrator of this group."
    if await _is_protected_bot_admin_or_owner(user_id, chat_id):
        return "Cannot demote bot admin or owner."

    try:
        await client.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            privileges=pyrogram.types.ChatAdministratorRights(
                can_manage_chat=False,
                can_change_info=False,
                can_delete_messages=False,
                can_restrict_members=False,
                can_invite_users=False,
                can_pin_messages=False,
                can_manage_video_chats=False,
                can_manage_topics=False,
            ),
        )
    except RPCError as e:
        logger.warning(
            f"Failed to demote user {user_id} in chat {chat_id}: {e.__class__.__name__}: {e}"
        )
        return f"Failed to demote user {user_id}: {e.__class__.__name__}. The bot may lack promote/demote permissions."

    logger.warning(
        f"Agent demoted user {user_id} in chat {chat_id}; requested by {ctx.deps.user_id}; reason: {reason!r}"
    )
    return f"User {user_id} has been demoted to a regular member."


async def pin_chat_message(
    ctx: RunContext[datatype.ContextDeps],
    message_id: int | None = None,
    disable_notification: bool = False,
) -> str:
    """Pin a message in the current group.

    Args:
        message_id: Specific message ID to pin. If omitted and replying to a message, pins the replied message.
        disable_notification: If True, pins the message silently without notifying group members.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "pin message"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    if message_id is None and ctx.deps.message.reply_to_message:
        message_id = ctx.deps.message.reply_to_message.id

    if message_id is None:
        return "Please specify message_id or reply to a message to pin."

    try:
        await client.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=disable_notification,
        )
        return f"Successfully pinned message {message_id}."
    except RPCError as e:
        logger.warning(f"Failed to pin message {message_id} in {chat_id}: {e}")
        return f"Failed to pin message: {e.__class__.__name__}. The bot may lack pin permissions."


async def unpin_chat_message(
    ctx: RunContext[datatype.ContextDeps],
    message_id: int | None = None,
) -> str:
    """Unpin a message in the current group.

    Args:
        message_id: Specific message ID to unpin. If omitted, unpins the replied message. If there is no replied message, unpins the most recently pinned message.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "unpin message"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    if message_id is None and ctx.deps.message.reply_to_message:
        message_id = ctx.deps.message.reply_to_message.id

    try:
        if message_id is not None:
            await client.unpin_chat_message(chat_id=chat_id, message_id=message_id)
            return f"Successfully unpinned message {message_id}."
        else:
            await client.unpin_chat_message(chat_id=chat_id)
            return "Successfully unpinned the pinned message."
    except RPCError as e:
        logger.warning(f"Failed to unpin message in {chat_id}: {e}")
        return f"Failed to unpin message: {e.__class__.__name__}. The bot may lack pin permissions."


async def warn_user(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
    reason: str = "",
) -> str:
    """Warn a group member. If they reach 3 warnings, they are automatically muted for 24 hours.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or reply target.
        reason: Brief reason for warning this user.
    """
    user_id, _, refusal = await _resolve_checked_target_member(ctx, user_id, target, "warn")
    if refusal or user_id is None:
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    # Increment warning count
    key = f"warns:{chat_id}:{user_id}"
    warns = await common.memttlcache.get(key) or 0
    warns += 1
    await common.memttlcache.set(key, warns, ttl=30 * 86400)  # expires in 30 days

    if warns >= 3:
        # Reset warnings and mute
        await common.memttlcache.delete(key)
        until_date = datetime.now(UTC) + timedelta(hours=24)
        try:
            await client.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(),
                until_date=until_date,
            )
            return f"User {user_id} has been warned (3/3) and automatically muted for 24 hours. Reason: {reason}"
        except RPCError as e:
            logger.warning(f"Failed to mute user {user_id} on warning threshold in {chat_id}: {e}")
            return f"User {user_id} reached 3/3 warnings, but failed to mute them due to bot permission issues."
    
    return f"User {user_id} has been warned ({warns}/3). Reason: {reason}"


async def reset_user_warnings(
    ctx: RunContext[datatype.ContextDeps],
    user_id: int | None = None,
    target: str = "",
) -> str:
    """Reset warning count for a group member back to 0.

    Args:
        user_id: Telegram user ID when known.
        target: Optional @username, tg://user link, numeric ID, display name, or reply target.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "reset warnings"):
        return refusal

    user_id, refusal = await _resolve_moderation_target(ctx, user_id, target)
    if refusal or user_id is None:
        return refusal or "Cannot resolve target user."

    chat_id = ctx.deps.chat_id
    key = f"warns:{chat_id}:{user_id}"
    await common.memttlcache.delete(key)
    return f"Warnings for user {user_id} have been reset to 0."


async def set_slow_mode(
    ctx: RunContext[datatype.ContextDeps],
    seconds: int = 0,
) -> str:
    """Set or disable slow mode for the current group chat.

    Args:
        seconds: Delay in seconds that members must wait before sending another message. Set to 0 to disable slow mode.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "set slow mode"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    try:
        await client.set_slow_mode(chat_id, seconds)
        if seconds > 0:
            return f"Slow mode has been enabled. Members must wait {seconds} seconds between messages."
        else:
            return "Slow mode has been disabled."
    except RPCError as e:
        logger.warning(f"Failed to set slow mode in {chat_id}: {e}")
        return f"Failed to set slow mode: {e.__class__.__name__}. The bot may lack permission."


async def set_chat_permissions(
    ctx: RunContext[datatype.ContextDeps],
    send_messages: bool = True,
    send_media: bool = True,
    send_stickers: bool = True,
    send_gifs: bool = True,
    send_games: bool = True,
    send_inline: bool = True,
    embed_links: bool = True,
) -> str:
    """Set default permissions for all non-admin members in the current group.

    Args:
        send_messages: Allow members to send text messages.
        send_media: Allow members to send media (photos, videos, voice notes, documents).
        send_stickers: Allow members to send stickers.
        send_gifs: Allow members to send animations/GIFs.
        send_games: Allow members to send games.
        send_inline: Allow members to use inline bots.
        embed_links: Allow members to send links that embed preview.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "set chat permissions"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    try:
        await client.set_chat_permissions(
            chat_id,
            ChatPermissions(
                can_send_messages=send_messages,
                can_send_media_messages=send_media,
                can_send_other_messages=send_stickers or send_gifs or send_games or send_inline,
                can_add_web_page_previews=embed_links,
            )
        )
        return "Group permissions updated successfully."
    except RPCError as e:
        logger.warning(f"Failed to set chat permissions in {chat_id}: {e}")
        return f"Failed to set chat permissions: {e.__class__.__name__}. The bot may lack permissions."


async def set_chat_title(
    ctx: RunContext[datatype.ContextDeps],
    title: str,
) -> str:
    """Change the title of the current group chat.

    Args:
        title: The new title for the group chat.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "set chat title"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    try:
        await client.set_chat_title(chat_id, title)
        return f"Group title has been changed to '{title}'."
    except RPCError as e:
        logger.warning(f"Failed to set chat title in {chat_id}: {e}")
        return f"Failed to change group title: {e.__class__.__name__}. The bot may lack permission."


async def set_chat_description(
    ctx: RunContext[datatype.ContextDeps],
    description: str,
) -> str:
    """Change the description of the current group chat.

    Args:
        description: The new description for the group chat.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "set chat description"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    try:
        await client.set_chat_description(chat_id, description)
        return "Group description has been updated successfully."
    except RPCError as e:
        logger.warning(f"Failed to set chat description in {chat_id}: {e}")
        return f"Failed to change group description: {e.__class__.__name__}. The bot may lack permission."


async def lock_chat(
    ctx: RunContext[datatype.ContextDeps],
) -> str:
    """Lock the current group chat, preventing all non-admin members from sending messages.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "lock chat"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    try:
        await client.set_chat_permissions(
            chat_id,
            ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            )
        )
        return "Group chat has been locked. Only administrators can send messages."
    except RPCError as e:
        logger.warning(f"Failed to lock chat {chat_id}: {e}")
        return f"Failed to lock chat: {e.__class__.__name__}. The bot may lack permission."


async def unlock_chat(
    ctx: RunContext[datatype.ContextDeps],
) -> str:
    """Unlock the current group chat, restoring sending permissions for all members.
    """
    if refusal := await _ensure_group_management_allowed(ctx, "unlock chat"):
        return refusal

    chat_id = ctx.deps.chat_id
    client = ctx.deps.client

    try:
        await client.set_chat_permissions(
            chat_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        return "Group chat has been unlocked. All members can now send messages."
    except RPCError as e:
        logger.warning(f"Failed to unlock chat {chat_id}: {e}")
        return f"Failed to unlock chat: {e.__class__.__name__}. The bot may lack permission."


async def is_user_blocked(user_id: int) -> bool:
    """Check if a user is currently blocked from triggering the agent.

    Returns False if the user has block immunity active.
    """
    if await common.memttlcache.get(state.user_block_immune_key(user_id)):
        return False
    return bool(await common.memttlcache.get(state.user_blocked_key(user_id)))
