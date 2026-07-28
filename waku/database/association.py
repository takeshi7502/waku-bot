from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy
import sqlalchemy.dialects
import sqlalchemy.dialects.mysql
import sqlalchemy.dialects.postgresql
import sqlalchemy.dialects.sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from waku import enums
from waku.config import app_config, runtime_config

from .db import AsyncSessionFactory, with_session, with_tx
from .models import ChatData, UserChatAssociation, UserData
from .user import upsert_user

_association_cache: set[tuple[int, int]] = set()


@with_session
async def count_associations(session: AsyncSession | None = None):
    assert session is not None
    stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(UserChatAssociation)
    result = await session.execute(stmt)
    return result.scalar() or 0


@with_tx
async def add_association_in_chat(
    chat: ChatData,
    user: UserData,
    waifu: UserData | None = None,
    session: AsyncSession | None = None,
) -> UserChatAssociation | None:
    assert session is not None

    # 如果已知该 association 存在，跳过 upsert 直接返回
    cache_pair = (user.id, chat.id)
    if cache_pair in _association_cache:
        return await session.get(UserChatAssociation, (user.id, chat.id))

    if runtime_config.db_is_postgres:
        stmt = (
            sqlalchemy.dialects.postgresql.insert(UserChatAssociation)
            .values(
                user_id=user.id,
                chat_id=chat.id,
                waifu_id=waifu.id if waifu else None,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "chat_id"])
            .returning(UserChatAssociation)
        )
    elif runtime_config.db_is_mysql:
        stmt = (
            sqlalchemy.dialects.mysql.insert(UserChatAssociation)
            .values(
                user_id=user.id,
                chat_id=chat.id,
                waifu_id=waifu.id if waifu else None,
            )
            .prefix_with("IGNORE")
            .returning(UserChatAssociation)
        )
    elif runtime_config.db_is_sqlite:
        stmt = (
            sqlalchemy.dialects.sqlite.insert(UserChatAssociation)
            .values(
                user_id=user.id,
                chat_id=chat.id,
                waifu_id=waifu.id if waifu else None,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "chat_id"])
            .returning(UserChatAssociation)
        )
    else:
        if data := await session.get(UserChatAssociation, (user.id, chat.id)):
            _association_cache.add(cache_pair)
            return data
        member = UserChatAssociation(
            user_id=user.id,
            chat_id=chat.id,
            waifu_id=waifu.id if waifu else None,
        )
        session.add(member)
        _association_cache.add(cache_pair)
        return member

    result = await session.execute(stmt)
    association = result.scalars().first()
    _association_cache.add(cache_pair)
    if association is not None:
        return association
    return await session.get(UserChatAssociation, (user.id, chat.id))


@with_session
async def get_association(
    user_id: int, chat_id: int, session: AsyncSession | None = None
) -> UserChatAssociation | None:
    assert session is not None
    return await session.get(UserChatAssociation, (user_id, chat_id))


@with_session
async def get_user_chats(
    user_id: int, session: AsyncSession | None = None
) -> list[tuple[ChatData, bool]]:
    """Return chats associated with a user and whether they are a bot admin there."""
    assert session is not None

    stmt = (
        sqlalchemy.select(ChatData, UserChatAssociation.is_bot_admin)
        .join(UserChatAssociation, UserChatAssociation.chat_id == ChatData.id)
        .where(UserChatAssociation.user_id == user_id)
        .order_by(ChatData.updated_at.desc(), ChatData.id.desc())
    )
    result = await session.execute(stmt)
    return [(chat, bool(is_bot_admin)) for chat, is_bot_admin in result.all()]


@with_tx
async def update_association(
    association: UserChatAssociation,
    session: AsyncSession | None = None,
):
    assert session is not None

    stmt = (
        sqlalchemy.update(UserChatAssociation)
        .where(
            UserChatAssociation.user_id == association.user_id,
            UserChatAssociation.chat_id == association.chat_id,
        )
        .values(
            waifu_id=association.waifu_id,
            is_bot_admin=association.is_bot_admin,
            promoted_by=association.promoted_by,
        )
    )
    await session.execute(stmt)


@with_tx
async def remove_association(
    user_id: int, chat_id: int, session: AsyncSession | None = None
) -> bool:
    assert session is not None
    stmt = (
        sqlalchemy.delete(UserChatAssociation)
        .where(
            UserChatAssociation.user_id == user_id,
            UserChatAssociation.chat_id == chat_id,
        )
        .returning(UserChatAssociation.user_id)
    )
    result = await session.execute(stmt)
    deleted = result.scalars().first()
    if deleted is not None:
        _association_cache.discard((user_id, chat_id))
        return True
    return False


@with_session
async def get_user_waifu_in_chat(
    user: UserData, chat: ChatData, session: AsyncSession | None = None
) -> tuple[UserData | None, bool]:
    """get user waifu in chat
    if user is married, return married waifu

    Returns:
        - UserData | None: waifu
        - bool: return is married waifu
    """
    assert session is not None

    if user.married_waifu_id is not None:
        waifu = await session.get(UserData, user.married_waifu_id)
        if waifu is not None:
            return waifu, True
    association = await get_association(user.id, chat.id, session)
    if association is None or association.waifu_id is None:
        return None, False
    waifu = await session.get(UserData, association.waifu_id)
    return waifu, False


@with_session
async def get_setted_waifu_in_chat(
    user: UserData, chat: ChatData, session: AsyncSession | None = None
) -> UserData | None:
    """get user setted waifu in chat"""
    assert session is not None

    association = await get_association(user.id, chat.id, session)
    if association is None or association.waifu_id is None:
        return None
    waifu = await session.get(UserData, association.waifu_id)
    return waifu


@with_session
async def is_setted_waifu_in_chat(
    user: UserData, chat: ChatData, session: AsyncSession | None = None
) -> bool:
    """check if user waifu is set in chat"""
    association = await get_association(user.id, chat.id, session)
    if association is None:
        return False
    return association.waifu_id is not None


@with_tx
async def set_user_waifu_in_chat(
    user: UserData, chat: ChatData, waifu: UserData, session: AsyncSession | None = None
) -> bool:
    association = await get_association(user.id, chat.id, session)
    if association is None:
        raise ValueError("Association not found")
    if association.waifu_id is not None:
        raise ValueError("Waifu already set")
    association.waifu_id = waifu.id
    return True


@with_tx
async def unset_user_waifu_in_chat(
    user: UserData, chat: ChatData, session: AsyncSession | None = None
) -> bool:
    association = await get_association(user.id, chat.id, session)
    if association is None:
        raise ValueError("Association not found")
    if association.waifu_id is None:
        raise ValueError("Waifu not set")
    association.waifu_id = None
    return True


@with_tx
async def unset_chat_waifus_by_waifu(
    chat: ChatData, waifu_id: int, session: AsyncSession | None = None
):
    assert session is not None

    stmt = (
        sqlalchemy.update(UserChatAssociation)
        .where(
            UserChatAssociation.chat_id == chat.id,
            UserChatAssociation.waifu_id == waifu_id,
        )
        .values(waifu_id=None)
    )
    await session.execute(stmt)


@with_session
async def take_waifu_for_user_in_chat(
    user: UserData, chat: ChatData, session: AsyncSession | None = None
) -> UserData | None:
    assert session is not None

    excluded_user_ids = [
        user.id,
        enums.ChatID.ANONYMOUS_ADMIN,
        enums.ChatID.SERVICE_CHAT,
        enums.ChatID.FAKE_CHANNEL,
    ]

    stmt = (
        sqlalchemy.select(UserData)
        .join(
            UserChatAssociation,
            (UserChatAssociation.user_id == UserData.id)
            & (UserChatAssociation.chat_id == chat.id),
        )
        .where(
            sqlalchemy.and_(
                sqlalchemy.not_(UserData.is_bot),
                sqlalchemy.not_(UserData.is_married),
                sqlalchemy.not_(UserData.id.in_(excluded_user_ids)),
            )
        )
        .order_by(sqlalchemy.func.random())
        .limit(1)
    )

    result = await session.execute(stmt)
    waifu = result.scalars().first()

    return waifu


def get_chat_user_participated_waifu(
    chat_id: int, batch_size: int = 100
) -> AsyncGenerator[UserData]:
    async def user_generator():
        async with AsyncSessionFactory() as session:
            offset = 0

            while True:
                users_with_waifu_stmt = (
                    sqlalchemy.select(UserData.id)
                    .join(
                        UserChatAssociation,
                        (UserChatAssociation.user_id == UserData.id)
                        & (UserChatAssociation.chat_id == chat_id),
                    )
                    .where(UserChatAssociation.waifu_id.isnot(None))
                )

                users_as_waifu_stmt = sqlalchemy.select(
                    UserChatAssociation.waifu_id
                ).where(
                    (UserChatAssociation.chat_id == chat_id)
                    & (UserChatAssociation.waifu_id.isnot(None))
                )

                union_stmt = sqlalchemy.union(
                    users_with_waifu_stmt, users_as_waifu_stmt
                ).subquery()

                stmt = (
                    sqlalchemy.select(UserData)
                    .where(UserData.id.in_(sqlalchemy.select(union_stmt.c.id)))
                    .offset(offset)
                    .limit(batch_size)
                )

                result = await session.execute(stmt)
                batch = result.scalars().all()

                if not batch:
                    break

                for user in batch:
                    yield user

                offset += batch_size

    return user_generator()


@with_session
async def count_chat_waifu_participants(
    chat_id: int, session: AsyncSession | None = None
) -> int:
    assert session is not None

    users_with_waifu_stmt = (
        sqlalchemy.select(UserData.id)
        .join(
            UserChatAssociation,
            (UserChatAssociation.user_id == UserData.id)
            & (UserChatAssociation.chat_id == chat_id),
        )
        .where(UserChatAssociation.waifu_id.isnot(None))
    )

    users_as_waifu_stmt = sqlalchemy.select(UserChatAssociation.waifu_id).where(
        (UserChatAssociation.chat_id == chat_id)
        & (UserChatAssociation.waifu_id.isnot(None))
    )

    stmt = sqlalchemy.union(users_with_waifu_stmt, users_as_waifu_stmt).subquery()

    count_stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(stmt)
    count_result = await session.execute(count_stmt)
    return count_result.scalar() or 0


@with_tx
async def cleanup_waifu_data(session: AsyncSession | None = None):
    assert session is not None

    stmt = (
        sqlalchemy.update(UserChatAssociation)
        .where(UserChatAssociation.waifu_id.isnot(None))
        .values(waifu_id=None)
    )
    await session.execute(stmt)


@with_session
async def get_chat_associations(
    chat_id: int, session: AsyncSession | None = None
) -> Sequence[UserChatAssociation]:
    assert session is not None

    stmt = sqlalchemy.select(UserChatAssociation).where(
        UserChatAssociation.chat_id == chat_id
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@with_tx
async def change_user_waifu_in_chat(
    user_id: int,
    chat: ChatData,
    cost: int = app_config.cost_user_change_waifu_base,
    session: AsyncSession | None = None,
) -> UserData | None:
    assert session is not None

    user = await session.get(UserData, user_id)
    if user is None:
        raise ValueError("User not found")
    association = await get_association(user.id, chat.id, session)
    if association is None:
        raise ValueError("Association not found")
    association.waifu_id = None
    config = user.user_config
    if config.coins < 0:
        raise ValueError("Not enough coins")
    config.coins = max(-144 * 16, config.coins - cost)
    user.user_config = config
    new_waifu = await take_waifu_for_user_in_chat(user, chat, session)
    if new_waifu is None:
        return None
    association.waifu_id = new_waifu.id
    return new_waifu


def _member_status_value(member: Any) -> str | None:
    status = getattr(member, "status", None)
    if status is None:
        return None
    return getattr(status, "value", str(status))


def _member_is_admin(member: Any) -> bool:
    status = _member_status_value(member)
    return status in {"owner", "administrator"}


def _member_privileges(member: Any) -> dict | None:
    privileges = getattr(member, "privileges", None)
    if privileges is None:
        privileges = getattr(member, "permissions", None)
    if privileges is None:
        return None
    data: dict[str, Any] = {}
    for key, value in vars(privileges).items():
        if key.startswith("_"):
            continue
        if isinstance(value, (bool, int, str)) or value is None:
            data[key] = value
    return data


def _member_tag(member: Any) -> str | None:
    return getattr(member, "custom_title", None) or getattr(member, "title", None)


@with_tx
async def upsert_member_snapshot(
    chat: ChatData,
    member: Any,
    session: AsyncSession | None = None,
) -> UserChatAssociation | None:
    """Store a Telegram chat member snapshot for a group."""
    assert session is not None
    user = getattr(member, "user", None)
    if user is None or getattr(user, "id", None) is None:
        return None
    db_user = await upsert_user(user, session)
    association = await get_association(db_user.id, chat.id, session)
    if association is None:
        association = UserChatAssociation(user_id=db_user.id, chat_id=chat.id)
        session.add(association)
        _association_cache.add((db_user.id, chat.id))
    association.member_status = _member_status_value(member)
    association.member_tag = _member_tag(member)
    association.member_is_admin = _member_is_admin(member)
    association.member_privileges = _member_privileges(member)
    association.last_member_sync_at = datetime.now().astimezone()
    return association


@with_session
async def get_chat_member_snapshots(
    chat_id: int,
    include_bots: bool = False,
    session: AsyncSession | None = None,
) -> Sequence[tuple[UserChatAssociation, UserData]]:
    assert session is not None
    stmt = (
        sqlalchemy.select(UserChatAssociation, UserData)
        .join(UserData, UserChatAssociation.user_id == UserData.id)
        .where(UserChatAssociation.chat_id == chat_id)
        .order_by(UserData.full_name.asc(), UserData.id.asc())
    )
    if not include_bots:
        stmt = stmt.where(sqlalchemy.not_(UserData.is_bot))
    result = await session.execute(stmt)
    return result.all()


@with_session
async def get_chat_member_snapshot_map(
    chat_id: int,
    session: AsyncSession | None = None,
) -> dict[int, tuple[str | None, str | None, bool]]:
    """Return compact stored member fields used to skip unchanged sync writes."""
    assert session is not None
    stmt = sqlalchemy.select(
        UserChatAssociation.user_id,
        UserChatAssociation.member_status,
        UserChatAssociation.member_tag,
        UserChatAssociation.member_is_admin,
    ).where(UserChatAssociation.chat_id == chat_id)
    result = await session.execute(stmt)
    return {
        user_id: (status, tag, bool(is_admin))
        for user_id, status, tag, is_admin in result.all()
    }


@dataclass
class MemberResolveResult:
    user_id: int | None
    matches: list[tuple[int, str, str | None, str | None]]
    reason: str | None = None


@with_session
async def resolve_member_by_name(
    chat_id: int,
    target: str,
    session: AsyncSession | None = None,
) -> MemberResolveResult:
    """Resolve a stored group member by username, full name, or member tag."""
    assert session is not None
    normalized = target.strip().casefold().lstrip("@")
    if not normalized:
        return MemberResolveResult(None, [], "empty")
    stmt = (
        sqlalchemy.select(UserData, UserChatAssociation)
        .join(
            UserChatAssociation,
            (UserChatAssociation.user_id == UserData.id)
            & (UserChatAssociation.chat_id == chat_id),
        )
        .where(sqlalchemy.not_(UserData.is_bot))
    )
    result = await session.execute(stmt)
    rows = list(result.all())

    def as_match(row) -> tuple[int, str, str | None, str | None]:
        user, assoc = row
        return (user.id, user.full_name, user.username, assoc.member_tag)

    exact_username = [
        row for row in rows if row[0].username and row[0].username.casefold() == normalized
    ]
    if len(exact_username) == 1:
        return MemberResolveResult(exact_username[0][0].id, [as_match(exact_username[0])])
    if len(exact_username) > 1:
        return MemberResolveResult(None, [as_match(row) for row in exact_username], "ambiguous")

    exact_name = [row for row in rows if row[0].full_name.casefold() == normalized]
    if len(exact_name) == 1:
        return MemberResolveResult(exact_name[0][0].id, [as_match(exact_name[0])])
    if len(exact_name) > 1:
        return MemberResolveResult(None, [as_match(row) for row in exact_name], "ambiguous")

    exact_tag = [
        row
        for row in rows
        if row[1].member_tag and row[1].member_tag.casefold() == normalized
    ]
    if len(exact_tag) == 1:
        return MemberResolveResult(exact_tag[0][0].id, [as_match(exact_tag[0])])
    if len(exact_tag) > 1:
        return MemberResolveResult(None, [as_match(row) for row in exact_tag], "ambiguous")

    partial = [
        row
        for row in rows
        if normalized in row[0].full_name.casefold()
        or (row[0].username and normalized in row[0].username.casefold())
        or (row[1].member_tag and normalized in row[1].member_tag.casefold())
    ]
    if len(partial) == 1:
        return MemberResolveResult(partial[0][0].id, [as_match(partial[0])])
    if len(partial) > 1:
        return MemberResolveResult(None, [as_match(row) for row in partial], "ambiguous")
    return MemberResolveResult(None, [], "not_found")


@with_tx
async def mark_gay_mode_tag(
    chat_id: int,
    user_id: int,
    previous_tag: str | None,
    session: AsyncSession | None = None,
) -> None:
    assert session is not None
    association = await session.get(UserChatAssociation, (user_id, chat_id))
    if association is None:
        return
    association.gay_mode_previous_tag = previous_tag
    association.gay_mode_applied = True


@with_session
async def get_gay_mode_applied_members(
    chat_id: int,
    session: AsyncSession | None = None,
) -> Sequence[tuple[UserChatAssociation, UserData]]:
    assert session is not None
    stmt = (
        sqlalchemy.select(UserChatAssociation, UserData)
        .join(UserData, UserChatAssociation.user_id == UserData.id)
        .where(
            UserChatAssociation.chat_id == chat_id,
            UserChatAssociation.gay_mode_applied.is_(True),
        )
        .order_by(UserData.full_name.asc(), UserData.id.asc())
    )
    result = await session.execute(stmt)
    return result.all()


@with_tx
async def clear_gay_mode_state(
    chat_id: int,
    user_id: int,
    restored_tag: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    assert session is not None
    association = await session.get(UserChatAssociation, (user_id, chat_id))
    if association is None:
        return
    association.member_tag = restored_tag
    association.gay_mode_previous_tag = None
    association.gay_mode_applied = False
