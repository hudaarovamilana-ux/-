"""ИИ-ответы на вопросы беременных через DeepSeek API."""

from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
DEEPSEEK_API_URL = os.getenv(
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/v1/chat/completions",
).strip()

SYSTEM_PROMPT = """Ты — доброжелательный ИИ-помощник в Telegram-боте «Женская консультация» для беременных.

Правила:
- Отвечай только на русском языке, тепло и понятно.
- Учитывай срок беременности, если он указан.
- Не ставь диагнозы, не назначай лекарства и дозировки.
- При тревожных симптомах (кровотечение, сильная боль, отсутствие шевелений, высокое давление и т.п.) — чётко рекомендуй срочно обратиться к врачу или в скорую.
- Ответ — 2–4 коротких абзаца, без markdown-заголовков и без символов * _ ` которые ломают разметку.
- В конце мягко напомни, что живой ответ гинеколога придёт в этот чат позже.
- Твой ответ помогает успокоиться сейчас, но не заменяет консультацию врача."""


def is_ai_configured() -> bool:
    return bool(DEEPSEEK_API_KEY)


async def generate_pregnancy_answer(
    question: str,
    week: int | None = None,
    pregnancy_day: int | None = None,
) -> str | None:
    """Возвращает текст ответа ИИ или None, если API недоступен."""
    if not DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY is empty")
        return None

    user_content = question.strip()
    if week is not None:
        d = pregnancy_day or 0
        user_content = (
            f"Срок беременности (акушерский): {week} недель {d} дней.\n\n"
            f"Вопрос: {question.strip()}"
        )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.6,
        "max_tokens": 800,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.error("DeepSeek API error %s: %s", resp.status, body[:800])
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                return str(content).strip()
    except Exception as exc:  # noqa: BLE001
        logger.exception("DeepSeek request failed: %s", exc)
        return None
