from collections.abc import Sequence

import sqlalchemy
import sqlalchemy.orm
from sqlalchemy.ext.asyncio import AsyncSession

from waku.config import app_config, runtime_config
from waku.database.db import with_session, with_tx
from waku.database.models import ChatData, Quote, UserChatAssociation, UserData


def _build_text_search_condition(query: str) -> sqlalchemy.ColumnElement[bool]:
    """
    根据数据库类型和配置构建文本搜索条件。

    对于 PostgreSQL:
    - 如果启用了 PGroonga (pg_pgroonga=true)，使用 &@~ 操作符进行全文搜索
    - 否则使用 pg_trgm 的 ilike

    对于其他数据库:
    - 使用标准的 ilike
    """
    if runtime_config.db_is_postgres and app_config.pg_pgroonga:
        # PGroonga 全文搜索操作符 &@~
        return Quote.text.op("&@~")(query)
    else:
        # 标准 ILIKE 查询（SQLite, MySQL, PostgreSQL with pg_trgm）
        return Quote.text.ilike(f"%{query}%")


@with_session
async def count_quotes(
    session: AsyncSession | None = None,
) -> int:
    assert session is not None

    stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(Quote)
    result = await session.execute(stmt)
    return result.scalar_one() or 0


@with_session
async def get_quote_by_link(
    link: str, session: AsyncSession | None = None
) -> Quote | None:
    assert session is not None

    return await session.get(Quote, link)


@with_tx
async def add_quote(
    chat: ChatData,
    user: UserData,
    qer: UserData,
    link: str,
    message_id: int,
    text: str | None = None,
    img: str | None = None,
    session: AsyncSession | None = None,
):
    assert session is not None

    if await session.get(Quote, link):
        return
    quote = Quote(
        chat_id=chat.id,
        user_id=user.id,
        message_id=message_id,
        link=link,
        qer_id=qer.id,
        text=text,
        img=img,
    )
    session.add(quote)


@with_session
async def get_chat_random_quote(
    chat_id: int, with_text: bool = False, session: AsyncSession | None = None
) -> Quote | None:
    assert session is not None

    conditions: list = [Quote.chat_id == chat_id]
    if with_text:
        conditions.append(Quote.text.is_not(None))

    stmt = (
        sqlalchemy.select(Quote)
        .options(sqlalchemy.orm.selectinload(Quote.user))
        .where(*conditions)
        .order_by(sqlalchemy.func.random())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


@with_tx
async def delete_quote(link: str, session: AsyncSession | None = None) -> None:
    assert session is not None

    quote = await session.get(Quote, link)
    await session.delete(quote)


@with_session
async def take_quotes_user_can_see(
    user_id: int,
    query: str = "",
    limit: int = 50,
    session: AsyncSession | None = None,
) -> Sequence[Quote]:
    assert session is not None

    conditions = [
        Quote.text.is_not(None),
        sqlalchemy.or_(
            Quote.chat_id.in_(
                sqlalchemy.select(ChatData.id).join(
                    UserChatAssociation,
                    (UserChatAssociation.chat_id == ChatData.id)
                    & (UserChatAssociation.user_id == user_id),
                )
            ),
            Quote.user_id == user_id,
            Quote.qer_id == user_id,
        ),
    ]

    if query:
        conditions.append(_build_text_search_condition(query))

    stmt = (
        sqlalchemy.select(Quote)
        .options(sqlalchemy.orm.selectinload(Quote.user))
        .where(*conditions)
        .order_by(sqlalchemy.func.random())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@with_session
async def get_user_quote_count(
    user_id: int, session: AsyncSession | None = None
) -> int:
    assert session is not None

    stmt = sqlalchemy.select(sqlalchemy.func.count()).where(Quote.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one()


@with_session
async def get_user_quotes_page(
    user_id: int, page: int, page_size: int, session: AsyncSession | None = None
) -> Sequence[Quote]:
    assert session is not None

    stmt = (
        sqlalchemy.select(Quote)
        .where(Quote.user_id == user_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@with_session
async def get_chat_quotes(
    chat_id: int,
    query: str = "",
    limit: int = 50,
    session: AsyncSession | None = None,
) -> Sequence[Quote]:
    assert session is not None

    conditions = [
        Quote.text.is_not(other=None),
        Quote.chat_id == chat_id,
    ]

    if query:
        conditions.append(_build_text_search_condition(query))

    stmt = (
        sqlalchemy.select(Quote)
        .options(sqlalchemy.orm.selectinload(Quote.user))
        .where(*conditions)
        .order_by(sqlalchemy.func.random())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()

from dataclasses import dataclass as _dataclass

@_dataclass(slots=True)
class PageResult:
    items: list
    total: int
    page: int
    size: int


@with_session
async def get_user_quotes_paged(
    user_id: int,
    page: int = 1,
    size: int = 20,
    session: AsyncSession | None = None,
) -> PageResult:
    assert session is not None
    conditions = [Quote.user_id == user_id]
    total_stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(Quote).where(*conditions)
    total = (await session.execute(total_stmt)).scalar_one() or 0
    stmt = (
        sqlalchemy.select(Quote)
        .options(sqlalchemy.orm.selectinload(Quote.user))
        .where(*conditions)
        .order_by(Quote.created_at.desc(), Quote.link.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return PageResult(items=list(rows), total=total, page=page, size=size)


@with_session
async def get_chat_quotes_paged(
    chat_id: int,
    page: int = 1,
    size: int = 20,
    query: str = '',
    session: AsyncSession | None = None,
) -> PageResult:
    assert session is not None
    conditions = [Quote.chat_id == chat_id]
    if query:
        conditions.append(_build_text_search_condition(query))
    total_stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(Quote).where(*conditions)
    total = (await session.execute(total_stmt)).scalar_one() or 0
    stmt = (
        sqlalchemy.select(Quote)
        .options(sqlalchemy.orm.selectinload(Quote.user))
        .where(*conditions)
        .order_by(Quote.created_at.desc(), Quote.link.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return PageResult(items=list(rows), total=total, page=page, size=size)


@with_session
async def count_chat_quotes(chat_id: int, session: AsyncSession | None = None) -> int:
    assert session is not None
    stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(Quote).where(Quote.chat_id == chat_id)
    return (await session.execute(stmt)).scalar_one() or 0


@with_session
async def count_telegram_quotes(session: AsyncSession | None = None) -> int:
    """Count quotes in Telegram chats only, excluding Discord bridge rows."""
    assert session is not None
    stmt = sqlalchemy.select(Quote, ChatData).join(ChatData, Quote.chat_id == ChatData.id)
    result = await session.execute(stmt)
    return sum(1 for _quote, chat in result.all() if not chat.title.startswith("Discord DM ") and not chat.chat_config.discord_enabled)
