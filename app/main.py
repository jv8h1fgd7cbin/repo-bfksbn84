"""Точка входа: запускает Telethon-монитор и админ-панель в одном процессе."""
import asyncio
import logging

import uvicorn

from app.admin.web import app as admin_app
from app.config import settings
from app.db.database import init_db
from app.telegram.manager import manager

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    await manager.start_all()  # поднимает мониторы всех авторизованных аккаунтов
    server = uvicorn.Server(
        uvicorn.Config(
            admin_app,
            host=settings.admin_panel_host,
            port=settings.admin_panel_port,
            log_level=settings.log_level.lower(),
        )
    )
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
