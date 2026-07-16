"""Админ-панель: статистика, поиск, экспорт."""
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.db.database import SessionMaker
from app.db.models import Category
from app.services import daily_limit, exporter, repository

app = FastAPI(title="Pet Owner Finder Admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MEDIA_TYPES = {
    "csv": ("text/csv", exporter.to_csv),
    "json": ("application/json", exporter.to_json),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", exporter.to_xlsx),
}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    async with SessionMaker() as session:
        stats = await repository.dashboard_stats(session)
    stats["new_today"] = await daily_limit.new_users_today()
    stats["processed_last_hour"] = await daily_limit.processed_last_hour()
    stats["joins_today"] = await daily_limit.joins_today()
    return templates.TemplateResponse(request, "dashboard.html", {"stats": stats})


@app.get("/api/stats")
async def api_stats():
    async with SessionMaker() as session:
        stats = await repository.dashboard_stats(session)
    stats["new_today"] = await daily_limit.new_users_today()
    stats["processed_last_hour"] = await daily_limit.processed_last_hour()
    stats["joins_today"] = await daily_limit.joins_today()
    return stats


@app.get("/api/search")
async def api_search(
    username: str | None = None,
    user_id: int | None = None,
    name: str | None = None,
    category: Category | None = None,
    chat_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(200, le=1000),
):
    async with SessionMaker() as session:
        owners = await repository.search_users(
            session, username, user_id, name, category, chat_id, date_from, date_to, limit
        )
    return exporter.to_json(owners)


@app.get("/api/export/{category}/{fmt}")
async def api_export(category: str, fmt: str):
    if fmt not in MEDIA_TYPES:
        return Response("unknown format", status_code=400)
    cat = None if category == "all" else Category(category)
    async with SessionMaker() as session:
        owners = await repository.search_users(session, category=cat, limit=100000)
    media_type, fn = MEDIA_TYPES[fmt]
    filename = f"pet_owners_{category}.{fmt}"
    return Response(
        fn(owners),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
