"""Один Telegram-аккаунт с серверным входом (QR / номер+код) и мониторингом."""
import asyncio
import logging
import uuid
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import settings
from app.telegram.monitor import PetFinderMonitor

logger = logging.getLogger(__name__)


class PendingLogin:
    """Незавершённый вход: клиент создан, но ещё не авторизован."""

    def __init__(self, client: TelegramClient, method: str) -> None:
        self.client = client
        self.method = method  # "qr" | "phone"
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.qr = None


class AccountManager:
    """Обслуживает единственный аккаунт: вход, статус, мониторинг. Один экземпляр на процесс."""

    def __init__(self) -> None:
        self.monitor: PetFinderMonitor | None = None
        self.task: asyncio.Task | None = None
        self.pending: dict[str, PendingLogin] = {}
        self._session_name = settings.telegram_session_name

    def _new_client(self) -> TelegramClient:
        return TelegramClient(
            self._session_name, settings.telegram_api_id, settings.telegram_api_hash
        )

    # ------------------------------------------------------------- lifecycle

    async def start_all(self) -> None:
        """Если сессия уже авторизована — поднимает монитор; иначе ждём входа через панель."""
        client = self._new_client()
        await client.connect()
        if await client.is_user_authorized():
            await self._start_monitor(client)
        else:
            await client.disconnect()
            logger.warning("No authorized session — log in via /login")

    async def _start_monitor(self, client: TelegramClient) -> None:
        self.monitor = PetFinderMonitor(client=client, label=self._session_name)
        self.task = asyncio.create_task(self.monitor.run())
        logger.info("Monitoring started")

    async def status(self) -> dict:
        if not self.monitor:
            return {"logged_in": False}
        try:
            me = await self.monitor.client.get_me()
        except Exception:
            return {"logged_in": False}
        if not me:
            return {"logged_in": False}
        return {
            "logged_in": True,
            "user_id": me.id,
            "name": " ".join(filter(None, [me.first_name, me.last_name])) or None,
            "username": me.username,
            "phone": me.phone,
            "running": bool(self.task and not self.task.done()),
        }

    async def logout(self) -> None:
        """Останавливает монитор и удаляет сессию (полный выход из аккаунта)."""
        if self.monitor:
            try:
                await self.monitor.client.log_out()
            except Exception:
                logger.exception("log_out failed")
            await self.monitor.stop()
            if self.task and not self.task.done():
                self.task.cancel()
            self.monitor = None
            self.task = None
        f = Path(f"{self._session_name}.session")
        if f.exists():
            f.unlink()

    async def _promote(self, token: str) -> None:
        p = self.pending.pop(token, None)
        if not p:
            return
        await self._start_monitor(p.client)

    # ------------------------------------------------------------- QR login

    async def qr_start(self) -> dict:
        if self.monitor:  # уже вошли — не создаём вторую сессию поверх той же
            return {"error": "already_logged_in"}
        client = self._new_client()
        await client.connect()
        qr = await client.qr_login()
        token = uuid.uuid4().hex
        p = PendingLogin(client, "qr")
        p.qr = qr
        self.pending[token] = p
        return {"token": token, "url": qr.url}

    async def qr_poll(self, token: str) -> dict:
        p = self.pending.get(token)
        if not p or p.qr is None:
            return {"status": "unknown"}
        try:
            await p.qr.wait(timeout=1)
        except asyncio.TimeoutError:
            try:
                await p.qr.recreate()
            except Exception:
                pass
            return {"status": "waiting", "url": p.qr.url}
        except SessionPasswordNeededError:
            return {"status": "password", "token": token}
        await self._promote(token)
        return {"status": "authorized"}

    # ------------------------------------------------------------- phone login

    async def phone_start(self, phone: str) -> dict:
        if self.monitor:
            return {"error": "already_logged_in"}
        client = self._new_client()
        await client.connect()
        sent = await client.send_code_request(phone)
        token = uuid.uuid4().hex
        p = PendingLogin(client, "phone")
        p.phone = phone
        p.phone_code_hash = sent.phone_code_hash
        self.pending[token] = p
        return {"token": token}

    async def phone_code(self, token: str, code: str) -> dict:
        p = self.pending.get(token)
        if not p:
            return {"status": "unknown"}
        try:
            await p.client.sign_in(phone=p.phone, code=code, phone_code_hash=p.phone_code_hash)
        except SessionPasswordNeededError:
            return {"status": "password", "token": token}
        await self._promote(token)
        return {"status": "authorized"}

    async def phone_password(self, token: str, password: str) -> dict:
        p = self.pending.get(token)
        if not p:
            return {"status": "unknown"}
        await p.client.sign_in(password=password)
        await self._promote(token)
        return {"status": "authorized"}


manager = AccountManager()
