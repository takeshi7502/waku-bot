import sqlalchemy
import sqlalchemy.dialects
import sqlalchemy.dialects.mysql
import sqlalchemy.dialects.postgresql
import sqlalchemy.dialects.sqlite
from pyrogram.types import Chat
from sqlalchemy.ext.asyncio import AsyncSession

from waku.common.memory_store import memttlcache
from waku.config import runtime_config

from .db import with_session, with_tx
from .models import ChatConfig, ChatData, UserData

# 本地内存缓存：记录已同步到 DB 的群组快照，避免每条消息触发重复 upsert
# key: chat_id, value: (title, username)
_upsert_chat_cache: dict[int, tuple] = {}

_CHAT_CONFIG_CACHE_TTL = 300  # 5 分钟
_CHAT_CONFIG_CACHE_PREFIX = "chat_config:"


def _is_telegram_chat(chat: ChatData) -> bool:
    """Exclude Discord bridge pseudo-chats from Telegram Mini App data."""
    if chat.title.startswith("Discord DM "):
        return False
    try:
        if chat.chat_config.discord_enabled:
            return False
    except Exception:
        pass
    return True


@with_session
async def count_chats(session: AsyncSession | None = None) -> int:
    assert session is not None

    stmt = sqlalchemy.select(ChatData)
    result = await session.execute(stmt)
    return sum(1 for chat in result.scalars().all() if _is_telegram_chat(chat))


@with_session
async def list_known_telegram_groups(
    limit: int = 50, session: AsyncSession | None = None
) -> list[ChatData]:
    assert session is not None

    stmt = (
        sqlalchemy.select(ChatData)
        .where(ChatData.id < 0)
        .order_by(ChatData.updated_at.desc(), ChatData.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    groups: list[ChatData] = []
    for chat in rows:
        config = chat.chat_config
        if config.discord_enabled or chat.title.startswith("Discord DM "):
            continue
        groups.append(chat)
    return groups


@with_tx
async def upsert_chat(chat: Chat, session: AsyncSession | None = None) -> ChatData:
    assert session is not None

    if chat.id is None:
        raise ValueError("chat.id must not be None")
    chat_id: int = chat.id

    # 检查缓存：如果数据没有变化，直接从 DB 读取并返回，避免触发写事务
    cache_data = (chat.title, chat.username)
    if _upsert_chat_cache.get(chat_id) == cache_data:
        cached = await session.get(ChatData, chat_id)
        if cached is not None:
            return cached

    if runtime_config.db_is_postgres:
        stmt = (
            sqlalchemy.dialects.postgresql.insert(ChatData)
            .values(
                id=chat_id,
                title=chat.title,
                username=chat.username,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "title": chat.title,
                    "username": chat.username,
                },
            )
            .returning(ChatData)
        )
    elif runtime_config.db_is_mysql:
        stmt = (
            sqlalchemy.dialects.mysql.insert(ChatData)
            .values(
                id=chat_id,
                title=chat.title,
                username=chat.username,
            )
            .on_duplicate_key_update(
                title=chat.title,
                username=chat.username,
            )
            .returning(ChatData)
        )
    elif runtime_config.db_is_sqlite:
        stmt = (
            sqlalchemy.dialects.sqlite.insert(ChatData)
            .values(
                id=chat_id,
                title=chat.title,
                username=chat.username,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "title": chat.title,
                    "username": chat.username,
                },
            )
            .returning(ChatData)
        )
    else:
        chat_data = await session.get(ChatData, chat_id)
        if chat_data is None:
            chat_data = ChatData(
                id=chat_id,
                title=chat.title,
                username=chat.username,
            )
            session.add(chat_data)
        else:
            chat_data.title = chat.title or ""
            chat_data.username = chat.username
        _upsert_chat_cache[chat_id] = cache_data
        return chat_data

    result = await session.execute(stmt)
    chat_data = result.scalars().first()
    if chat_data is not None:
        _upsert_chat_cache[chat_id] = cache_data
        return chat_data

    data = await session.get(ChatData, chat_id)
    assert data is not None
    _upsert_chat_cache[chat_id] = cache_data
    return data


@with_session
async def get_chat_by_id(
    chat_id: int, session: AsyncSession | None = None
) -> ChatData | None:
    assert session is not None

    chat_data = await session.get(ChatData, chat_id)
    if chat_data is None:
        return None
    return chat_data


async def get_chat_config(chat: int | ChatData | Chat) -> ChatConfig:
    if isinstance(chat, ChatData):
        config = chat.chat_config
        await memttlcache.set(
            f"{_CHAT_CONFIG_CACHE_PREFIX}{chat.id}", config, _CHAT_CONFIG_CACHE_TTL
        )
        return config

    chat_id: int
    if isinstance(chat, Chat):
        if chat.id is None:
            raise ValueError("chat.id must not be None")
        chat_id = chat.id
    elif isinstance(chat, int):
        chat_id = chat
    else:
        raise TypeError("chat must be int, ChatData or Chat")

    cached = await memttlcache.get(f"{_CHAT_CONFIG_CACHE_PREFIX}{chat_id}")
    if cached is not None:
        return cached

    chat_data: ChatData | None = None
    if isinstance(chat, Chat):
        chat_data = await upsert_chat(chat)
    else:
        chat_data = await get_chat_by_id(chat_id)

    if chat_data is None:
        raise ValueError("Chat not found")

    config = chat_data.chat_config
    await memttlcache.set(
        f"{_CHAT_CONFIG_CACHE_PREFIX}{chat_id}", config, _CHAT_CONFIG_CACHE_TTL
    )
    return config


@with_tx
async def update_chat_config(
    chat: int | ChatData | Chat, config: ChatConfig, session: AsyncSession | None = None
) -> ChatConfig:
    assert session is not None

    chat_id = 0
    if isinstance(chat, ChatData):
        chat_id = chat.id
    elif isinstance(chat, Chat):
        if chat.id is None:
            raise ValueError("chat.id must not be None")
        chat_id = chat.id
    elif isinstance(chat, int):
        chat_id = chat
    else:
        raise TypeError("chat must be int, ChatData or Chat")

    chat_data = await session.get(ChatData, chat_id)
    if chat_data is None:
        if isinstance(chat, Chat):
            chat_data = ChatData(
                id=chat_id,
                title=chat.title,
                username=chat.username,
            )
            session.add(chat_data)
            await session.flush()
        else:
            raise ValueError(f"Chat with id {chat_id} not found")
    chat_data.chat_config = config

    # 立即更新缓存，使新配置对后续请求即时生效
    await memttlcache.set(
        f"{_CHAT_CONFIG_CACHE_PREFIX}{chat_id}", config, _CHAT_CONFIG_CACHE_TTL
    )

    return chat_data.chat_config


from dataclasses import dataclass as _dataclass
from .models import UserChatAssociation, Quote

@_dataclass(slots=True)
class PageResult:
    items: list
    total: int
    page: int
    size: int


@with_session
async def get_chats_page(
    page: int = 1,
    size: int = 20,
    query: str = "",
    session: AsyncSession | None = None,
) -> PageResult:
    assert session is not None
    conditions = []
    if query:
        pattern = f"%{query}%"
        query_conditions = [ChatData.title.ilike(pattern), ChatData.username.ilike(pattern)]
        try:
            query_conditions.append(ChatData.id == int(query))
        except ValueError:
            pass
        conditions.append(sqlalchemy.or_(*query_conditions))
    stmt = (
        sqlalchemy.select(ChatData)
        .where(*conditions)
        .order_by(ChatData.updated_at.desc(), ChatData.id.desc())
    )
    rows_all = [
        chat for chat in (await session.execute(stmt)).scalars().all()
        if _is_telegram_chat(chat)
    ]
    total = len(rows_all)
    start = (page - 1) * size
    rows = rows_all[start : start + size]
    return PageResult(items=list(rows), total=total, page=page, size=size)


@with_session
async def count_chat_members(chat_id: int, session: AsyncSession | None = None) -> int:
    assert session is not None
    stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(UserChatAssociation).where(UserChatAssociation.chat_id == chat_id)
    return (await session.execute(stmt)).scalar_one() or 0


@with_tx
async def delete_chat(chat_id: int, session: AsyncSession | None = None) -> bool:
    assert session is not None
    chat = await session.get(ChatData, chat_id)
    if chat is None:
        return False
    await session.delete(chat)
    return True


@with_session
async def get_chat_bot_admins(
    chat_id: int, session: AsyncSession | None = None
) -> list[tuple[UserData, int | None]]:
    assert session is not None
    stmt = (
        sqlalchemy.select(UserData, UserChatAssociation.promoted_by)
        .join(UserChatAssociation, UserChatAssociation.user_id == UserData.id)
        .where(
            UserChatAssociation.chat_id == chat_id,
            UserChatAssociation.is_bot_admin.is_(True),
        )
        .order_by(UserData.full_name.asc(), UserData.id.asc())
    )
    return [(user, promoted_by) for user, promoted_by in (await session.execute(stmt)).all()]
