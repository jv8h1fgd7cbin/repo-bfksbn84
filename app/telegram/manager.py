"""Один Telegram-аккаунт с серверным входом (QR / номер+код) и мониторингом."""
import asyncio
import logging
import time
import uuid
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import settings
from app.db.database import SessionMaker
from app.services import daily_limit, repository
from app.telegram.monitor import PetFinderMonitor

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 600  # заброшенные незавершённые входы очищаются через 10 минут


class PendingLogin:
    """Незавершённый вход: клиент создан, но ещё не авторизован."""

    def __init__(self, client: TelegramClient, method: str) -> None:
        self.client = client
        self.method = method  # "qr" | "phone"
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.qr = None
        self.created_at = time.monotonic()


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
            me = await client.get_me()
            if me:
                await self._switch_account(me.id)
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

    async def _sweep_pending(self) -> None:
        """Отключает и удаляет заброшенные незавершённые входы (утечка соединений)."""
        now = time.monotonic()
        for token, p in list(self.pending.items()):
            if now - p.created_at > PENDING_TTL_SECONDS:
                self.pending.pop(token, None)
                try:
                    await p.client.disconnect()
                except Exception:
                    logger.exception("Failed to disconnect stale pending login")

    async def _close_other_pending(self, keep_token: str) -> None:
        """Отключает остальные незавершённые входы: они держат тот же session-файл и блокируют его."""
        for tok, other in list(self.pending.items()):
            if tok == keep_token:
                continue
            self.pending.pop(tok, None)
            try:
                await other.client.disconnect()
            except Exception:
                logger.exception("Failed to disconnect competing pending login")

    async def _promote(self, token: str) -> bool:
        """Завершает вход. Если задан ADMIN_USER_ID — впускает только этот аккаунт.

        При входе с другого аккаунта данные предыдущего стираются (чистый лист)."""
        await self._close_other_pending(token)
        p = self.pending.pop(token, None)
        if not p:
            return False
        try:
            me = await p.client.get_me()
        except Exception:
            me = None
        if not me:
            return False
        if settings.admin_user_id and me.id != settings.admin_user_id:
            logger.warning("Login rejected: account is not the configured admin")
            try:
                await p.client.log_out()
            except Exception:
                logger.exception("Failed to log out non-admin account")
            return False
        await self._switch_account(me.id)
        await self._start_monitor(p.client)
        return True

    async def _switch_account(self, user_id: int) -> None:
        """Если вошли под другим аккаунтом — очищаем данные предыдущего и начинаем с нуля."""
        previous = await daily_limit.get_account()
        if previous is not None and previous != user_id:
            logger.info("Account changed %s -> %s, wiping previous data", previous, user_id)
            async with SessionMaker() as session:
                await repository.wipe_all(session)
                await session.commit()
            await daily_limit.reset_state()
        await daily_limit.set_account(user_id)

    # ------------------------------------------------------------- QR login

    async def qr_start(self) -> dict:
        await self._sweep_pending()
        if self.monitor:  # уже вошли — не создаём вторую сессию поверх той же
            return {"error": "already_logged_in"}
        await self._close_other_pending("")  # один активный вход за раз, иначе блокировка session
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
        ok = await self._promote(token)
        return {"status": "authorized"} if ok else {"status": "forbidden"}

    # ------------------------------------------------------------- phone login

    async def phone_start(self, phone: str) -> dict:
        await self._sweep_pending()
        if self.monitor:
            return {"error": "already_logged_in"}
        await self._close_other_pending("")  # один активный вход за раз, иначе блокировка session
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
        ok = await self._promote(token)
        return {"status": "authorized"} if ok else {"status": "forbidden"}

    async def phone_password(self, token: str, password: str) -> dict:
        p = self.pending.get(token)
        if not p:
            return {"status": "unknown"}
        await p.client.sign_in(password=password)
        ok = await self._promote(token)
        return {"status": "authorized"} if ok else {"status": "forbidden"}


manager = AccountManager()
