import re
from datetime import UTC, datetime, timedelta

import sqlalchemy
from pydantic_ai import RunContext
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import RPCError
from pyrogram.types import ChatPermissions

from waku import common, database
from waku.affection import get_affection_rank
from waku.config import app_config
from waku.database.db import AsyncSessionFactory
from waku.database.models import UserChatAssociation, UserData
from waku.logger import logger

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
    """Resolve a group member by username/full name only when the match is safe."""
    normalized = target.strip().lower().lstrip("@")
    if not normalized:
        return None, None

    async with AsyncSessionFactory() as session:
        stmt = (
            sqlalchemy.select(UserData)
            .join(
                UserChatAssociation,
                (UserChatAssociation.user_id == UserData.id)
                & (UserChatAssociation.chat_id == chat_id),
            )
            .where(sqlalchemy.not_(UserData.is_bot))
        )
        result = await session.execute(stmt)
        users = list(result.scalars().all())

    exact_username = [
        user
        for user in users
        if user.username and user.username.lower() == normalized
    ]
    if len(exact_username) == 1:
        return exact_username[0].id, None
    if len(exact_username) > 1:
        return None, "Multiple users match that username. Provide a user ID or reply to the target message."

    exact_name = [
        user
        for user in users
        if user.full_name and user.full_name.lower() == normalized
    ]
    if len(exact_name) == 1:
        return exact_name[0].id, None
    if len(exact_name) > 1:
        return None, "Multiple users match that name. Reply to the target message, mention @username, or provide user ID."

    partial_name = [
        user
        for user in users
        if user.full_name and normalized in user.full_name.lower()
    ]
    if len(partial_name) == 1:
        return partial_name[0].id, None
    if len(partial_name) > 1:
        return None, "Multiple users match that name. Reply to the target message, mention @username, or provide user ID."

    return None, "Cannot resolve target user. Reply to their message, mention @username, or provide user ID."


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


async def is_user_blocked(user_id: int) -> bool:
    """Check if a user is currently blocked from triggering the agent.

    Returns False if the user has block immunity active.
    """
    if await common.memttlcache.get(state.user_block_immune_key(user_id)):
        return False
    return bool(await common.memttlcache.get(state.user_blocked_key(user_id)))
