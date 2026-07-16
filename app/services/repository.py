"""Работа с БД: upsert пользователей (ключ user_id), история сообщений, поиск, статистика."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, ChatStatus, MonitoredChat, PetOwner, SystemEvent, UserMessage


async def upsert_user(
    session: AsyncSession,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    message_date: datetime,
) -> None:
    stmt = pg_insert(PetOwner).values(
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        last_message_at=message_date,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PetOwner.user_id],
        set_={
            "username": stmt.excluded.username,
            "first_name": stmt.excluded.first_name,
            "last_name": stmt.excluded.last_name,
            "last_message_at": func.greatest(
                func.coalesce(PetOwner.last_message_at, stmt.excluded.last_message_at),
                stmt.excluded.last_message_at,
            ),
        },
    )
    await session.execute(stmt)


async def add_message(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    chat_name: str | None,
    message_id: int,
    message_date: datetime,
    text: str,
) -> bool:
    stmt = pg_insert(UserMessage).values(
        user_id=user_id,
        chat_id=chat_id,
        chat_name=chat_name,
        message_id=message_id,
        message_date=message_date,
        text=text,
    )
    stmt = stmt.on_conflict_do_nothing(constraint="uq_chat_message")
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def get_user_messages(session: AsyncSession, user_id: int, limit: int = 50) -> list[str]:
    rows = await session.execute(
        select(UserMessage.text)
        .where(UserMessage.user_id == user_id)
        .order_by(UserMessage.message_date.desc())
        .limit(limit)
    )
    return [r[0] for r in reversed(rows.all())]


async def update_category(
    session: AsyncSession, user_id: int, category: Category, confidence: float
) -> None:
    owner = await session.get(PetOwner, user_id)
    if owner:
        owner.category = category
        owner.confidence = confidence


async def upsert_chat(
    session: AsyncSession,
    chat_id: int,
    title: str | None,
    username: str | None,
    status: ChatStatus,
    reason: str | None = None,
) -> None:
    link = f"https://t.me/{username}" if username else None
    stmt = pg_insert(MonitoredChat).values(
        chat_id=chat_id, title=title, username=username, link=link, status=status, reason=reason
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[MonitoredChat.chat_id],
        set_={"title": stmt.excluded.title, "username": stmt.excluded.username,
              "link": stmt.excluded.link, "status": stmt.excluded.status,
              "reason": stmt.excluded.reason},
    )
    await session.execute(stmt)


async def remove_pending_duplicates(session: AsyncSession, username: str, keep_chat_id: int) -> None:
    """Удаляет устаревшие PENDING-записи с тем же username после появления реального чата."""
    await session.execute(
        delete(MonitoredChat).where(
            MonitoredChat.username == username,
            MonitoredChat.chat_id != keep_chat_id,
            MonitoredChat.status == ChatStatus.PENDING_ACCESS,
        )
    )


async def set_last_message_id(session: AsyncSession, chat_id: int, last_message_id: int) -> None:
    chat = await session.get(MonitoredChat, chat_id)
    if chat and last_message_id > (chat.last_message_id or 0):
        chat.last_message_id = last_message_id


async def log_event(session: AsyncSession, event_type: str, detail: str) -> None:
    session.add(SystemEvent(event_type=event_type, detail=detail))


async def search_users(
    session: AsyncSession,
    username: str | None = None,
    user_id: int | None = None,
    name: str | None = None,
    category: Category | None = None,
    chat_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 200,
) -> list[PetOwner]:
    query = select(PetOwner)
    if username:
        query = query.where(PetOwner.username.ilike(f"%{username}%"))
    if user_id:
        query = query.where(PetOwner.user_id == user_id)
    if name:
        query = query.where(
            (PetOwner.first_name.ilike(f"%{name}%")) | (PetOwner.last_name.ilike(f"%{name}%"))
        )
    if category:
        query = query.where(PetOwner.category == category)
    if chat_id or date_from or date_to:
        sub = select(UserMessage.user_id)
        if chat_id:
            sub = sub.where(UserMessage.chat_id == chat_id)
        if date_from:
            sub = sub.where(UserMessage.message_date >= date_from)
        if date_to:
            sub = sub.where(UserMessage.message_date <= date_to)
        query = query.where(PetOwner.user_id.in_(sub))
    rows = await session.execute(query.order_by(PetOwner.updated_at.desc()).limit(limit))
    return list(rows.scalars())


async def dashboard_stats(session: AsyncSession) -> dict:
    def count_category(cat: Category):
        return select(func.count()).select_from(PetOwner).where(PetOwner.category == cat)

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {
        "chats_active": (await session.execute(
            select(func.count()).select_from(MonitoredChat).where(MonitoredChat.status == ChatStatus.ACTIVE)
        )).scalar_one(),
        "chats_pending": (await session.execute(
            select(func.count()).select_from(MonitoredChat).where(MonitoredChat.status == ChatStatus.PENDING_ACCESS)
        )).scalar_one(),
        "dog_owners": (await session.execute(count_category(Category.DOG))).scalar_one(),
        "cat_owners": (await session.execute(count_category(Category.CAT))).scalar_one(),
        "dog_and_cat_owners": (await session.execute(count_category(Category.DOG_AND_CAT))).scalar_one(),
        "undefined": (await session.execute(count_category(Category.UNDEFINED))).scalar_one(),
        "total_users": (await session.execute(select(func.count()).select_from(PetOwner))).scalar_one(),
        "new_today_db": (await session.execute(
            select(func.count()).select_from(PetOwner).where(PetOwner.first_seen_at >= today)
        )).scalar_one(),
        "errors_24h": (await session.execute(
            select(func.count()).select_from(SystemEvent).where(
                SystemEvent.event_type == "error",
                SystemEvent.created_at >= datetime.now(timezone.utc) - timedelta(hours=24),
            )
        )).scalar_one(),
        "floodwait_24h": (await session.execute(
            select(func.count()).select_from(SystemEvent).where(
                SystemEvent.event_type == "floodwait",
                SystemEvent.created_at >= datetime.now(timezone.utc) - timedelta(hours=24),
            )
        )).scalar_one(),
    }
    return stats
