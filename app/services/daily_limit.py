"""Суточный лимит новых уникальных пользователей (только новые user_id)."""
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis

from app.config import settings

_redis = aioredis.from_url(settings.redis_url, decode_responses=True)

# Атомарная регистрация нового пользователя в суточном лимите (без гонки SCARD/SADD).
_REGISTER_LUA = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 1 end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], 172800)
return 1
"""
_register_script = _redis.register_script(_REGISTER_LUA)


def _key() -> str:
    return f"new_users:{datetime.now(timezone.utc):%Y-%m-%d}"


async def try_register_new_user(user_id: int) -> bool:
    """Регистрирует нового пользователя в суточном счётчике.

    Возвращает False, если лимит на сегодня исчерпан. Повторные user_id
    не увеличивают счётчик. Атомарно (Lua), поэтому лимит не превышается
    даже при параллельной обработке."""
    result = await _register_script(keys=[_key()], args=[user_id, settings.daily_new_users_limit])
    return bool(result)


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


async def queue_failed_analysis(user_id: int) -> None:
    """Откладывает пользователя для повторного ИИ-анализа после сбоя ИИ."""
    await _redis.sadd("failed_analyses", user_id)


_POP_LUA = """
local m = redis.call('SMEMBERS', KEYS[1])
redis.call('DEL', KEYS[1])
return m
"""
_pop_script = _redis.register_script(_POP_LUA)


async def pop_failed_analyses() -> list[int]:
    """Возвращает и очищает отложенных пользователей (атомарно, без гонки чтение/удаление)."""
    members = await _pop_script(keys=["failed_analyses"])
    return [int(m) for m in members]


def _minute_key(dt: datetime) -> str:
    return f"processed:{dt:%Y-%m-%d-%H-%M}"


async def incr_processed() -> None:
    key = _minute_key(datetime.now(timezone.utc))
    await _redis.incr(key)
    await _redis.expire(key, 60 * 90)


async def processed_last_hour() -> int:
    """Скользящее окно: сумма обработанных сообщений за последние 60 минут."""
    now = datetime.now(timezone.utc)
    keys = [_minute_key(now - timedelta(minutes=i)) for i in range(60)]
    vals = await _redis.mget(keys)
    return sum(int(v) for v in vals if v)
