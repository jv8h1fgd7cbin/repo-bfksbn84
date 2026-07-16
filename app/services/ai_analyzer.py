"""Классификация пользователей по сообщениям с учётом контекста нескольких сообщений."""
import json
import logging

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.db.models import Category

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=settings.openai_api_key)

SYSTEM_PROMPT = """Ты — аналитик, определяющий владельцев домашних животных по сообщениям в Telegram-чатах.

Тебе дают ВСЕ известные сообщения одного пользователя. Определи, является ли он владельцем собаки, кошки, обоих, или это невозможно определить.

Правила:
- Анализируй совокупность сообщений, а не отдельные слова.
- Никогда не делай уверенный вывод по одному короткому сообщению: если сообщение всего одно, уверенность не может быть выше 60.
- Признаки владения: "мой кот", "чем кормить щенка", "повёз кота к ветеринару", "у нас появился котёнок", рассказы об уходе, кормлении, лечении, кличках своих животных.
- НЕ считай владельцем: обсуждение чужих животных, мемы, продажа товаров, ветеринары/грумеры без собственных питомцев, гипотетические вопросы.

Ответ строго в JSON:
{"category": "dog" | "cat" | "dog_and_cat" | "undefined", "confidence": 0-100, "reason": "краткое объяснение"}
category = недоминирующая категория только если есть явные признаки владения. confidence — уверенность в категории в процентах."""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=20))
async def _call_openai(prompt: str) -> str:
    resp = await _client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or "{}"


async def analyze_user(messages: list[str]) -> tuple[Category, float, str]:
    """Возвращает (категория, уверенность 0-100, объяснение) по всем сообщениям пользователя."""
    numbered = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(messages[-settings.context_messages_count :]))
    prompt = f"Сообщения пользователя ({len(messages)} всего, последние ниже):\n{numbered}"
    try:
        raw = await _call_openai(prompt)
        data = json.loads(raw)
        category = Category(data.get("category", "undefined"))
        confidence = max(0.0, min(100.0, float(data.get("confidence", 0))))
        reason = str(data.get("reason", ""))
    except Exception:
        logger.exception("AI analysis failed, falling back to undefined")
        return Category.UNDEFINED, 0.0, "ai_error"

    if len(messages) < settings.min_messages_for_category and confidence > 60:
        confidence = 60.0
    return category, confidence, reason


PRELIMINARY_KEYWORDS = (
    "собак", "щен", "пёс", "пса", "псу", "кот", "кош", "котен", "котён",
    "питом", "ветеринар", "корм", "лапа", "хвост", "шпиц", "овчарк",
    "лабрадор", "мейн-кун", "сфинкс", "поводок", "лоток", "будк",
)


def looks_pet_related(text: str) -> bool:
    """Быстрый предварительный фильтр перед вызовом ИИ (экономия токенов)."""
    lowered = text.lower()
    return any(k in lowered for k in PRELIMINARY_KEYWORDS)
