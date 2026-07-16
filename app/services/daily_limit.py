"""Суточный лимит новых уникальных пользователей (только новые user_id)."""
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.config import settings

_redis = aioredis.from_url(settings.redis_url, decode_responses=True)


def _key() -> str:
    return f"new_users:{datetime.now(timezone.utc):%Y-%m-%d}"


async def try_register_new_user(user_id: int) -> bool:
    """Регистрирует нового пользователя в суточном счётчике.

    Возвращает False, если лимит на сегодня исчерпан. Повторные user_id
    не увеличивают счётчик (SADD идемпотентен)."""
    key = _key()
    if await _redis.sismember(key, user_id):
        return True
    if await _redis.scard(key) >= settings.daily_new_users_limit:
        return False
    added = await _redis.sadd(key, user_id)
    if added:
        await _redis.expire(key, 60 * 60 * 48)
    return True


async def new_users_today() -> int:
    return await _redis.scard(_key())


async def is_known_user(user_id: int) -> bool:
    return bool(await _redis.sismember("known_users", user_id))


async def mark_known_user(user_id: int) -> None:
    await _redis.sadd("known_users", user_id)


async def joins_today() -> int:
    val = await _redis.get(f"joins:{datetime.now(timezone.utc):%Y-%m-%d}")
    return int(val or 0)


async def incr_joins() -> None:
    key = f"joins:{datetime.now(timezone.utc):%Y-%m-%d}"
    await _redis.incr(key)
    await _redis.expire(key, 60 * 60 * 48)


async def is_discovered_chat(identifier: str) -> bool:
    """Уже обрабатывали этот чат в авто-поиске (чтобы не дёргать повторно)."""
    return bool(await _redis.sismember("discovered_chats", identifier))


async def mark_discovered_chat(identifier: str) -> None:
    await _redis.sadd("discovered_chats", identifier)


async def incr_processed() -> None:
    await _redis.incr(f"processed:{datetime.now(timezone.utc):%Y-%m-%d-%H}")
    await _redis.expire(f"processed:{datetime.now(timezone.utc):%Y-%m-%d-%H}", 60 * 60 * 25)


async def processed_last_hour() -> int:
    val = await _redis.get(f"processed:{datetime.now(timezone.utc):%Y-%m-%d-%H}")
    return int(val or 0)
