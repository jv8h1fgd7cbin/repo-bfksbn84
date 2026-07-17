"""Классификация пользователей по сообщениям с учётом контекста нескольких сообщений.

Поддерживаются провайдеры: openai и anthropic (AI_PROVIDER в .env).
"""
import asyncio
import json
import logging
import re
import time

from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.db.models import Category

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — аналитик, определяющий владельцев домашних животных по сообщениям в Telegram-чатах.

Тебе дают ВСЕ известные сообщения одного пользователя. Определи, является ли он владельцем собаки, кошки, обоих, или это невозможно определить.

Правила:
- Анализируй совокупность сообщений, а не отдельные слова.
- Никогда не делай уверенный вывод по одному короткому сообщению: если сообщение всего одно, уверенность не может быть выше 60.
- Признаки владения: "мой кот", "чем кормить щенка", "повёз кота к ветеринару", "у нас появился котёнок", рассказы об уходе, кормлении, лечении, кличках своих животных.
- НЕ считай владельцем: обсуждение чужих животных, мемы, продажа товаров, ветеринары/грумеры без собственных питомцев, гипотетические вопросы.

Ответ строго в JSON без пояснений вне JSON:
{"category": "dog" | "cat" | "dog_and_cat" | "undefined", "confidence": 0-100, "reason": "краткое объяснение"}
category — не undefined только если есть явные признаки владения. confidence — уверенность в категории в процентах."""

# Троттлинг: не чаще одного запроса к ИИ раз в ai_min_interval_seconds
_throttle_lock = asyncio.Lock()
_last_call_at = 0.0


async def _throttle() -> None:
    global _last_call_at
    async with _throttle_lock:
        wait = settings.ai_min_interval_seconds - (time.monotonic() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = time.monotonic()


if settings.ai_provider == "anthropic":
    import httpx

    _base_url = (settings.anthropic_base_url or "https://api.anthropic.com").rstrip("/")
    _http = httpx.AsyncClient(timeout=120)

    async def _call_llm_with_system(system: str, prompt: str) -> str:
        """Запрос к ИИ с троттлингом и backoff: на 429 ждём Retry-After (или
        экспоненциально), на 5xx/сетевые ошибки — повтор с паузой."""
        last_exc: Exception | None = None
        for attempt in range(settings.ai_retry_max_attempts):
            await _throttle()
            try:
                resp = await _http.post(
                    f"{_base_url}/v1/messages",
                    headers={
                        "x-api-key": settings.anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": settings.anthropic_model,
                        "max_tokens": 512,
                        "system": system,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                    },
                )
            except httpx.HTTPError as e:
                last_exc = e
                await asyncio.sleep(min(5 * 2**attempt, 60))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                try:
                    retry_after = float(resp.headers.get("retry-after", "") or 0)
                except ValueError:
                    retry_after = 0
                if not retry_after:
                    # прокси пишет время ожидания в теле: "retry in about N seconds"
                    m = re.search(r"retry in about (\d+) seconds", resp.text)
                    if m:
                        retry_after = float(m.group(1)) + 5
                wait = min(retry_after, 600) or min(settings.ai_backoff_base_seconds * 2**attempt, 300)
                logger.warning("AI %s, waiting %.0fs (attempt %d/%d)",
                               resp.status_code, wait, attempt + 1, settings.ai_retry_max_attempts)
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code}", request=resp.request, response=resp
                )
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        raise last_exc or RuntimeError("AI retries exhausted")
else:
    from openai import AsyncOpenAI

    _openai = AsyncOpenAI(api_key=settings.openai_api_key)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=20))
    async def _call_llm_with_system(system: str, prompt: str) -> str:
        await _throttle()
        resp = await _openai.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or "{}"


def _extract_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    return json.loads(match.group(0) if match else raw)


GROUP_RELEVANCE_PROMPT = """Ты определяешь, посвящено ли Telegram-сообщество владельцам домашних животных (собак и/или кошек).

Тебе дают образец последних сообщений группы. Ответь, является ли эта группа тематическим сообществом про кошек/собак (владельцы, уход, породы, ветеринария, приюты, зоотовары и т.п.), где имеет смысл искать владельцев питомцев.

Ответ строго в JSON: {"relevant": true|false, "reason": "кратко"}"""


async def is_relevant_group(sample_messages: list[str]) -> tuple[bool, str]:
    """По образцу сообщений решает, релевантна ли группа тематике питомцев."""
    if not sample_messages:
        return False, "no_messages"
    sample = "\n".join(f"- {m}" for m in sample_messages[:40] if m)
    prompt = f"Образец сообщений группы:\n{sample}"
    try:
        raw = await _call_llm_with_system(GROUP_RELEVANCE_PROMPT, prompt)
        data = _extract_json(raw)
        return bool(data.get("relevant", False)), str(data.get("reason", ""))
    except Exception:
        logger.exception("Group relevance check failed")
        return False, "ai_error"


class AIUnavailableError(Exception):
    """ИИ временно недоступен — существующую категорию перезаписывать нельзя."""


async def analyze_user(messages: list[str]) -> tuple[Category, float, str]:
    """Возвращает (категория, уверенность 0-100, объяснение) по всем сообщениям пользователя."""
    numbered = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(messages[-settings.context_messages_count :]))
    prompt = f"Сообщения пользователя ({len(messages)} всего, последние ниже):\n{numbered}"
    try:
        raw = await _call_llm_with_system(SYSTEM_PROMPT, prompt)
        data = _extract_json(raw)
        category = Category(data.get("category", "undefined"))
        confidence = max(0.0, min(100.0, float(data.get("confidence", 0))))
        reason = str(data.get("reason", ""))
    except Exception as e:
        logger.exception("AI analysis failed")
        raise AIUnavailableError from e

    if len(messages) < settings.min_messages_for_category and confidence > 60:
        confidence = 60.0
    return category, confidence, reason


PRELIMINARY_PATTERN = re.compile(
    r"\b(собак\w*|щен(ок|ка|ки|ку|ком|очек|ят\w*)?|п[её]с\w{0,2}|"
    r"кот\w{0,3}|кош(ка|ки|ке|ку|кой|ек|ач\w*)|кот[её]н\w*|"
    r"питом\w*|ветеринар\w*|корм(а|ом|у|ить|лю|им)?|шпиц\w*|овчарк\w*|"
    r"лабрадор\w*|мейн-?кун\w*|сфинкс\w*|поводк\w*|поводок|лотк\w*|лоток|будк\w*)\b",
    re.IGNORECASE,
)


def looks_pet_related(text: str) -> bool:
    """Быстрый предварительный фильтр перед вызовом ИИ (экономия токенов).

    Совпадение по границам слов, чтобы «который», «которая» и т.п. не давали
    ложных срабатываний."""
    return bool(PRELIMINARY_PATTERN.search(text))
