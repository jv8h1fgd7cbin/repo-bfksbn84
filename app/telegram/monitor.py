"""Telethon-монитор: первоначальная загрузка истории и режим реального времени."""
import asyncio
import logging
import re

from telethon import TelegramClient, events
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError, FloodWaitError, InviteHashInvalidError
from telethon.tl.types import Channel, Chat, Message, User

from app.config import settings
from app.db.database import SessionMaker
from app.db.models import ChatStatus, MonitoredChat
from app.services import daily_limit, repository
from app.services.ai_analyzer import analyze_user, looks_pet_related
from app.telegram.discovery import Discovery

logger = logging.getLogger(__name__)

TME_LINK_RE = re.compile(r"(?:https?://)?t\.me/(joinchat/|\+)?([\w-]+)")
TME_RESERVED_PATHS = {"c", "s", "iv", "share", "proxy", "socks", "addstickers", "addemoji", "addtheme", "setlanguage", "joinchat"}


class PetFinderMonitor:
    def __init__(self) -> None:
        self.client = TelegramClient(
            settings.telegram_session_name, settings.telegram_api_id, settings.telegram_api_hash
        )
        self._analyze_lock = asyncio.Lock()
        self._discovery = Discovery(self.client, self._notify_admin, self._log)

    # ---------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Основной цикл с автоматическим восстановлением после сбоев."""
        while True:
            try:
                await self.client.start()
                logger.info("Telegram client started")
                self.client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
                await self._sync_dialogs()
                await self._backfill_all()
                asyncio.create_task(self._periodic_tasks())
                if settings.discovery_enabled:
                    asyncio.create_task(self._discovery_loop())
                await self.client.run_until_disconnected()
            except FloodWaitError as e:
                await self._log("floodwait", f"global floodwait {e.seconds}s")
                await asyncio.sleep(e.seconds + 5)
            except Exception:
                logger.exception("Monitor crashed, restarting in 15s")
                await self._log("error", "monitor crash, restart")
                await asyncio.sleep(15)

    async def _discovery_loop(self) -> None:
        """Периодический авто-поиск и авто-вступление в публичные группы."""
        while True:
            try:
                await self._discovery.run_once()
                await self._sync_dialogs()
                await self._backfill_all()
            except FloodWaitError as e:
                await self._log("floodwait", f"discovery loop floodwait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception:
                logger.exception("Discovery loop failed")
                await self._log("error", "discovery loop failed")
            await asyncio.sleep(settings.discovery_interval_seconds)

    async def _periodic_tasks(self) -> None:
        while True:
            await asyncio.sleep(settings.dialogs_refresh_seconds)
            try:
                await self._sync_dialogs()
                await self._check_pending_chats()
                await self._backfill_all()
            except FloodWaitError as e:
                await self._log("floodwait", f"periodic floodwait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception:
                logger.exception("Periodic task failed")
                await self._log("error", "periodic task failed")

    # ---------------------------------------------------------------- chats

    async def _sync_dialogs(self) -> None:
        """Регистрирует все доступные группы/супергруппы/форумы."""
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and entity.megagroup)
            if not is_group:
                continue
            async with SessionMaker() as session:
                await repository.upsert_chat(
                    session,
                    chat_id=dialog.id,
                    title=dialog.name,
                    username=getattr(entity, "username", None),
                    status=ChatStatus.ACTIVE,
                )
                await session.commit()

    async def _check_pending_chats(self) -> None:
        """Проверяет очередь чатов без доступа: если владелец вступил — начинаем индексацию."""
        from sqlalchemy import select

        async with SessionMaker() as session:
            rows = await session.execute(
                select(MonitoredChat).where(MonitoredChat.status == ChatStatus.PENDING_ACCESS)
            )
            pending = list(rows.scalars())
        for chat in pending:
            if not chat.username:
                continue
            try:
                entity = await self.client.get_entity(chat.username)
                messages = await self.client.get_messages(entity, limit=1)
                if messages is not None:
                    async with SessionMaker() as session:
                        await repository.upsert_chat(
                            session, chat.chat_id, chat.title, chat.username, ChatStatus.ACTIVE
                        )
                        await session.commit()
                    logger.info("Access gained to %s, will index", chat.title)
            except FloodWaitError:
                raise
            except Exception:
                # чат всё ещё недоступен (приватный, несуществующий username и т.п.) — ждём дальше
                continue

    async def handle_discovered_chat(self, identifier: str) -> None:
        """Обнаружен новый чат (например, ссылка в сообщении). Никогда не вступаем сами."""
        try:
            entity = await self.client.get_entity(identifier)
            await self.client.get_messages(entity, limit=1)
        except (ChannelPrivateError, ChatAdminRequiredError, InviteHashInvalidError, ValueError):
            async with SessionMaker() as session:
                await repository.upsert_chat(
                    session,
                    chat_id=hash(identifier) & 0x7FFFFFFFFFFF,
                    title=identifier,
                    username=identifier.lstrip("@"),
                    status=ChatStatus.PENDING_ACCESS,
                    reason="Нет доступа",
                )
                await session.commit()
            await self._notify_admin(
                "Обнаружена группа без доступа\n"
                f"Название: {identifier}\n"
                f"Username: @{identifier.lstrip('@')}\n"
                f"Ссылка: https://t.me/{identifier.lstrip('@')}\n"
                "Причина: Нет доступа\n"
                "Вступите вручную — индексация начнётся автоматически."
            )

    # ---------------------------------------------------------------- backfill

    async def _backfill_all(self) -> None:
        from sqlalchemy import select

        async with SessionMaker() as session:
            rows = await session.execute(
                select(MonitoredChat).where(
                    MonitoredChat.status == ChatStatus.ACTIVE, MonitoredChat.backfilled.is_(False)
                )
            )
            chats = list(rows.scalars())
        for chat in chats:
            try:
                await self._backfill_chat(chat)
            except FloodWaitError as e:
                await self._log("floodwait", f"backfill {chat.title}: {e.seconds}s")
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                logger.exception("Backfill failed for %s", chat.title)
                await self._log("error", f"backfill failed: {chat.title}")

    async def _backfill_chat(self, chat: MonitoredChat) -> None:
        logger.info("Backfilling %s", chat.title)
        max_id = 0
        async for message in self.client.iter_messages(chat.chat_id, limit=settings.history_backfill_limit):
            if isinstance(message, Message) and message.text:
                await self._process_message(message, chat.chat_id, chat.title)
            max_id = max(max_id, message.id)
        async with SessionMaker() as session:
            db_chat = await session.get(MonitoredChat, chat.chat_id)
            if db_chat:
                db_chat.backfilled = True
                db_chat.last_message_id = max_id
            await session.commit()
        logger.info("Backfill done for %s", chat.title)

    # ---------------------------------------------------------------- realtime

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            if not event.is_group:
                return
            chat = await event.get_chat()
            await self._process_message(event.message, event.chat_id, getattr(chat, "title", None))
        except FloodWaitError as e:
            await self._log("floodwait", f"realtime: {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception:
            logger.exception("Failed to process new message")
            await self._log("error", "realtime message processing failed")

    # ---------------------------------------------------------------- processing

    async def _process_message(self, message: Message, chat_id: int, chat_name: str | None) -> None:
        text = message.text
        if not text:
            return
        sender = await message.get_sender()
        if not isinstance(sender, User) or sender.bot:
            return
        await daily_limit.incr_processed()

        for match in TME_LINK_RE.finditer(text):
            candidate = match.group(2)
            # только публичные username (не invite-ссылки и не служебные пути t.me)
            if not match.group(1) and candidate.lower() not in TME_RESERVED_PATHS and len(candidate) >= 5:
                asyncio.create_task(self.handle_discovered_chat(candidate))

        if not looks_pet_related(text):
            return

        is_known = await daily_limit.is_known_user(sender.id)
        if not is_known:
            if not await daily_limit.try_register_new_user(sender.id):
                logger.debug("Daily new-user limit reached, skipping user %s", sender.id)
                return
            await daily_limit.mark_known_user(sender.id)

        async with SessionMaker() as session:
            await repository.upsert_user(
                session, sender.id, sender.username, sender.first_name, sender.last_name, message.date
            )
            is_new_message = await repository.add_message(
                session, sender.id, chat_id, chat_name, message.id, message.date, text
            )
            await session.commit()
        if is_new_message:
            await self._reanalyze_user(sender.id)

    async def _reanalyze_user(self, user_id: int) -> None:
        """ИИ пересматривает категорию по всей истории сообщений пользователя."""
        async with self._analyze_lock:
            async with SessionMaker() as session:
                messages = await repository.get_user_messages(session, user_id)
            if not messages:
                return
            category, confidence, reason = await analyze_user(messages)
            async with SessionMaker() as session:
                await repository.update_category(session, user_id, category, confidence)
                await session.commit()
            logger.info(
                "User %s -> %s (%.0f%%): %s", user_id, category.value, confidence, reason
            )

    # ---------------------------------------------------------------- helpers

    async def _notify_admin(self, text: str) -> None:
        try:
            target = settings.admin_user_id or "me"
            await self.client.send_message(target, text)
        except Exception:
            logger.exception("Failed to notify admin")

    async def _log(self, event_type: str, detail: str) -> None:
        try:
            async with SessionMaker() as session:
                await repository.log_event(session, event_type, detail)
                await session.commit()
        except Exception:
            logger.exception("Failed to log event")
