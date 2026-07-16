"""Авто-поиск публичных групп по ключевым словам и авто-вступление.

Соблюдает консервативные лимиты Telegram (число вступлений в сутки, паузы
между вступлениями, обработка FloodWait), чтобы снизить риск блокировки аккаунта.
Приватные/по-заявке чаты не вступаются автоматически — они попадают в очередь
с уведомлением администратору (см. handle_no_access).
"""
import asyncio
import logging
import random

from telethon import TelegramClient
from telethon.errors import (
    ChannelsTooMuchError,
    ChannelPrivateError,
    FloodWaitError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel

from app.config import settings
from app.db.database import SessionMaker
from app.db.models import ChatStatus
from app.services import daily_limit, repository
from app.services.ai_analyzer import is_relevant_group

logger = logging.getLogger(__name__)


class Discovery:
    def __init__(self, client: TelegramClient, notify_admin, log_event) -> None:
        self.client = client
        self._notify_admin = notify_admin
        self._log = log_event

    async def run_once(self) -> None:
        """Один проход авто-поиска по всем ключевым словам."""
        for keyword in settings.discovery_keyword_list:
            if await daily_limit.joins_today() >= settings.max_joins_per_day:
                logger.info("Daily join limit reached (%s), stopping discovery",
                            settings.max_joins_per_day)
                return
            try:
                await self._search_and_join(keyword)
            except FloodWaitError as e:
                logger.warning("FloodWait during discovery: %ss", e.seconds)
                await self._log("floodwait", f"discovery search: {e.seconds}s")
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                logger.exception("Discovery failed for keyword %s", keyword)
                await self._log("error", f"discovery failed: {keyword}")

    async def _search_and_join(self, keyword: str) -> None:
        result = await self.client(SearchRequest(q=keyword, limit=20))
        for chat in result.chats:
            if not isinstance(chat, Channel):
                continue
            # Группа для анализа: либо сам megagroup, либо связанная с каналом
            # группа обсуждений (у broadcast-каналов сообщения пишут только админы).
            group = chat if chat.megagroup else await self._linked_discussion(chat)
            if group is None:
                continue

            username = getattr(group, "username", None)
            identifier = username or str(group.id)
            if await daily_limit.is_discovered_chat(identifier):
                continue
            await daily_limit.mark_discovered_chat(identifier)

            if await daily_limit.joins_today() >= settings.max_joins_per_day:
                return

            # ИИ проверяет по образцу сообщений, что группа реально про питомцев
            relevant, reason = await self._check_relevance(group)
            if not relevant:
                logger.info("Skip %s — не про питомцев: %s", getattr(group, "title", identifier), reason)
                continue

            await self._join_chat(group, identifier)
            delay = random.randint(settings.join_delay_min_seconds, settings.join_delay_max_seconds)
            logger.info("Waiting %ss before next join (rate limit)", delay)
            await asyncio.sleep(delay)

    async def _check_relevance(self, group: Channel) -> tuple[bool, str]:
        """Читает последние сообщения публичной группы и спрашивает ИИ о релевантности."""
        try:
            texts = [
                m.text for m in await self.client.get_messages(group, limit=settings.relevance_sample_size)
                if getattr(m, "text", None)
            ]
        except FloodWaitError:
            raise
        except Exception:
            logger.exception("Cannot sample messages of %s", getattr(group, "title", "?"))
            return False, "cannot_read"
        return await is_relevant_group(texts)

    async def _linked_discussion(self, channel: Channel) -> Channel | None:
        """Возвращает связанную группу обсуждений broadcast-канала, если есть."""
        try:
            full = await self.client(GetFullChannelRequest(channel))
        except (FloodWaitError, ChannelPrivateError):
            raise
        except Exception:
            return None
        linked_id = getattr(full.full_chat, "linked_chat_id", None)
        if not linked_id:
            return None
        for c in full.chats:
            if isinstance(c, Channel) and c.id == linked_id and c.megagroup:
                return c
        return None

    async def _join_chat(self, chat: Channel, identifier: str) -> None:
        title = getattr(chat, "title", identifier)
        username = getattr(chat, "username", None)
        try:
            await self.client(JoinChannelRequest(chat))
            await daily_limit.incr_joins()
            async with SessionMaker() as session:
                await repository.upsert_chat(
                    session, chat.id, title, username, ChatStatus.ACTIVE
                )
                await session.commit()
            logger.info("Auto-joined group %s (@%s)", title, username)
        except FloodWaitError as e:
            logger.warning("FloodWait on join %s: %ss", title, e.seconds)
            await self._log("floodwait", f"join {title}: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
        except (ChannelPrivateError, InviteRequestSentError):
            await self._queue_no_access(chat.id, title, username, "Нет доступа")
        except UserAlreadyParticipantError:
            async with SessionMaker() as session:
                await repository.upsert_chat(session, chat.id, title, username, ChatStatus.ACTIVE)
                await session.commit()
        except ChannelsTooMuchError:
            logger.error("Account is in too many channels, cannot join more")
            await self._log("error", "too many channels: cannot auto-join")
        except Exception:
            logger.exception("Failed to join %s", title)
            await self._queue_no_access(chat.id, title, username, "Ошибка вступления")

    async def _queue_no_access(self, chat_id: int, title: str, username: str | None, reason: str) -> None:
        async with SessionMaker() as session:
            await repository.upsert_chat(
                session, chat_id, title, username, ChatStatus.PENDING_ACCESS, reason=reason
            )
            await session.commit()
        link = f"https://t.me/{username}" if username else "—"
        await self._notify_admin(
            "Нужно вступить вручную\n"
            f"Название: {title}\n"
            f"Username: @{username or '—'}\n"
            f"Ссылка: {link}\n"
            f"Причина: {reason}\n"
            "После вашего вступления индексация начнётся автоматически."
        )
