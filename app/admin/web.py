"""Админ-панель: вход через Telegram, статистика, поиск, экспорт, управление аккаунтами."""
import hashlib
import hmac
import io
from datetime import datetime
from pathlib import Path

import segno
from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.database import SessionMaker
from app.db.models import Category
from app.services import daily_limit, exporter, repository
from app.telegram.manager import manager

app = FastAPI(title="Pet Owner Finder Admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MEDIA_TYPES = {
    "csv": ("text/csv", exporter.to_csv),
    "json": ("application/json", exporter.to_json),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", exporter.to_xlsx),
}

COOKIE_NAME = "admin_session"
# публичные пути (доступны без входа)
PUBLIC_PATHS = {"/login", "/api/auth/qr/start", "/api/auth/qr/poll", "/api/auth/qr/image",
                "/api/auth/phone/start", "/api/auth/phone/code", "/api/auth/phone/password"}


def _secret() -> bytes:
    raw = settings.admin_secret or hashlib.sha256(settings.telegram_api_hash.encode()).hexdigest()
    return raw.encode()


def _make_cookie() -> str:
    return hmac.new(_secret(), b"authenticated", hashlib.sha256).hexdigest()


def _is_authenticated(request: Request) -> bool:
    if not settings.admin_auth_enabled:
        return True
    token = request.cookies.get(COOKIE_NAME, "")
    return bool(token) and hmac.compare_digest(token, _make_cookie())


def _authorize(response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME, _make_cookie(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
    )


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if settings.admin_auth_enabled and path not in PUBLIC_PATHS and not _is_authenticated(request):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


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
    limit: int = Query(50, le=1000),
    offset: int = Query(0, ge=0),
):
    async with SessionMaker() as session:
        total = await repository.count_search_users(
            session, username, user_id, name, category, chat_id, date_from, date_to
        )
        owners = await repository.search_users(
            session, username, user_id, name, category, chat_id, date_from, date_to, limit, offset
        )
    return {"count": total, "offset": offset, "limit": limit, "results": exporter.to_records(owners)}


@app.get("/api/chats")
async def api_chats():
    async with SessionMaker() as session:
        chats = await repository.list_chats(session)
    return {
        "chats": [
            {
                "chat_id": c.chat_id,
                "title": c.title,
                "username": c.username,
                "link": c.link,
                "status": c.status.value,
                "reason": c.reason,
            }
            for c in chats
        ]
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_authenticated(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/api/account")
async def api_account():
    return await manager.status()


@app.post("/api/account/logout")
async def api_account_logout():
    """Полный выход из Telegram-аккаунта: останавливает мониторинг и удаляет сессию."""
    await manager.logout()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.post("/api/auth/qr/start")
async def api_qr_start():
    return await manager.qr_start()


@app.get("/api/auth/qr/image")
async def api_qr_image(url: str):
    buff = io.BytesIO()
    segno.make(url, error="m").save(buff, kind="svg", scale=6)
    return Response(buff.getvalue(), media_type="image/svg+xml")


@app.get("/api/auth/qr/poll")
async def api_qr_poll(token: str):
    result = await manager.qr_poll(token)
    resp = JSONResponse(result)
    if result.get("status") == "authorized":
        _authorize(resp)
    return resp


@app.post("/api/auth/phone/start")
async def api_phone_start(phone: str = Body(..., embed=True)):
    return await manager.phone_start(phone)


@app.post("/api/auth/phone/code")
async def api_phone_code(token: str = Body(...), code: str = Body(...)):
    result = await manager.phone_code(token, code)
    resp = JSONResponse(result)
    if result.get("status") == "authorized":
        _authorize(resp)
    return resp


@app.post("/api/auth/phone/password")
async def api_phone_password(token: str = Body(...), password: str = Body(...)):
    result = await manager.phone_password(token, password)
    resp = JSONResponse(result)
    if result.get("status") == "authorized":
        _authorize(resp)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


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
