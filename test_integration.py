"""Интеграционный тест всех компонентов (кроме входа в Telegram-аккаунт)."""
import asyncio
from datetime import datetime, timezone

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name} {detail}")


async def test_db():
    from app.db.database import SessionMaker, init_db
    from app.db.models import Category
    from app.services import repository

    await init_db()
    check("DB: init/create tables", True)

    now = datetime.now(timezone.utc)
    async with SessionMaker() as s:
        await repository.upsert_user(s, 111, "ivan_pet", "Иван", None, now)
        await repository.upsert_user(s, 111, "ivan_pet_new", "Иван", "Петров", now)  # дубль
        added1 = await repository.add_message(s, 111, -100500, "Собачники Москвы", 1, now, "Чем кормить щенка?")
        added2 = await repository.add_message(s, 111, -100500, "Собачники Москвы", 1, now, "Чем кормить щенка?")  # дубль
        added3 = await repository.add_message(s, 111, -100500, "Собачники Москвы", 2, now, "Мой шпиц заболел")
        await s.commit()
        users = await repository.search_users(s, username="ivan_pet")
    check("DB: upsert без дублей (user_id ключ)", len(users) == 1 and users[0].username == "ivan_pet_new")
    check("DB: история сообщений + дедуп сообщений", added1 and not added2 and added3)

    async with SessionMaker() as s:
        msgs = await repository.get_user_messages(s, 111)
        await repository.update_category(s, 111, Category.DOG, 85.0)
        await s.commit()
        found = await repository.search_users(s, category=Category.DOG)
        stats = await repository.dashboard_stats(s)
    check("DB: поиск по категории", len(found) == 1 and found[0].confidence == 85.0)
    check("DB: статистика", stats["total_users"] >= 1 and stats["dog_owners"] >= 1, str(stats))
    return msgs


async def test_daily_limit():
    import redis.asyncio as aioredis

    from app.config import settings
    from app.services import daily_limit

    r = aioredis.from_url(settings.redis_url)
    await r.flushdb()
    ok_new = await daily_limit.try_register_new_user(1)
    ok_repeat = await daily_limit.try_register_new_user(1)
    for i in range(2, settings.daily_new_users_limit + 1):
        await daily_limit.try_register_new_user(i)
    over_limit = await daily_limit.try_register_new_user(99999)
    count = await daily_limit.new_users_today()
    check("Лимит: новые user_id считаются, повторы нет", ok_new and ok_repeat and count == settings.daily_new_users_limit)
    check(f"Лимит: {settings.daily_new_users_limit}-й+1 пользователь отклонён", over_limit is False)
    await r.flushdb()


async def test_exporter():
    from app.db.database import SessionMaker
    from app.services import exporter, repository

    async with SessionMaker() as s:
        owners = await repository.search_users(s)
    csv_data = exporter.to_csv(owners)
    json_data = exporter.to_json(owners)
    xlsx_data = exporter.to_xlsx(owners)
    check("Экспорт CSV/JSON/XLSX", b"ivan_pet_new" in csv_data and b"ivan_pet_new" in json_data and len(xlsx_data) > 1000)


async def test_ai():
    from app.db.models import Category
    from app.services.ai_analyzer import analyze_user, looks_pet_related

    check("ИИ-фильтр: pet-текст", looks_pet_related("у нас появился котёнок"))
    check("ИИ-фильтр: обычный текст", not looks_pet_related("продам гараж недорого"))

    cat, conf, reason = await analyze_user(["Чем кормить щенка?", "Мой шпиц вчера заболел", "Повёз пса к ветеринару"])
    check("ИИ: владелец собаки", cat == Category.DOG and conf > 50, f"{cat.value} {conf}% ({reason})")

    cat2, conf2, r2 = await analyze_user(["У кошки аллергия", "Мой кот Барсик обожает рыбу", "А ещё наш щенок сгрыз диван"])
    check("ИИ: кошки+собаки", cat2 == Category.DOG_AND_CAT, f"{cat2.value} {conf2}% ({r2})")

    cat3, conf3, r3 = await analyze_user(["Продам гараж", "Кто смотрел вчера футбол?"])
    check("ИИ: не владелец -> undefined", cat3 == Category.UNDEFINED, f"{cat3.value} {conf3}% ({r3})")

    cat4, conf4, r4 = await analyze_user(["кот"])
    check("ИИ: одно слово не даёт высокой уверенности", conf4 <= 60, f"{cat4.value} {conf4}% ({r4})")


async def test_telegram_connect():
    from telethon import TelegramClient

    from app.config import settings

    client = TelegramClient("test_conn", settings.telegram_api_id, settings.telegram_api_hash)
    await client.connect()
    authorized = await client.is_user_authorized()
    check("Telegram: MTProto-подключение с api_id/api_hash", client.is_connected(), f"authorized={authorized}")
    await client.disconnect()


async def test_admin_panel():
    import httpx
    from httpx import ASGITransport

    from app.admin.web import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r1 = await c.get("/")
        r2 = await c.get("/api/stats")
        r3 = await c.get("/api/search?category=dog")
        r4 = await c.get("/api/export/dog/csv")
        r5 = await c.get("/api/export/all/xlsx")
    check("Админ-панель: дашборд HTML", r1.status_code == 200 and "Владельцы собак" in r1.text)
    check("Админ-панель: /api/stats", r2.status_code == 200 and "total_users" in r2.text)
    check("Админ-панель: поиск", r3.status_code == 200)
    check("Админ-панель: экспорт CSV+XLSX", r4.status_code == 200 and r5.status_code == 200)


async def main():
    await test_db()
    await test_daily_limit()
    await test_exporter()
    await test_admin_panel()
    await test_telegram_connect()
    await test_ai()
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
