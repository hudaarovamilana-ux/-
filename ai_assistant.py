"""ИИ-ответы на вопросы беременных через OpenAI API."""

from __future__ import annotations

import os

import aiohttp

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

SYSTEM_PROMPT = """Ты — доброжелательный ИИ-помощник в Telegram-боте «Женская консультация» для беременных.

Правила:
- Отвечай только на русском языке, тепло и понятно.
- Учитывай срок беременности, если он указан.
- Не ставь диагнозы, не назначай лекарства и дозировки.
- При тревожных симптомах (кровотечение, сильная боль, отсутствие шевелений, высокое давление и т.п.) — чётко рекомендуй срочно обратиться к врачу или в скорую.
- Ответ — 2–4 коротких абзаца, без markdown-заголовков.
- В конце мягко напомни, что живой ответ гинеколога придёт в этот чат позже.
- Твой ответ помогает успокоиться сейчас, но не заменяет консультацию врача."""


async def generate_pregnancy_answer(
    question: str,
    week: int | None = None,
    pregnancy_day: int | None = None,
) -> str | None:
    """Возвращает текст ответа ИИ или None, если API недоступен."""
    if not OPENAI_API_KEY:
        return None

    user_content = question.strip()
    if week is not None:
        d = pregnancy_day or 0
        user_content = (
            f"Срок беременности (акушерский): {week} недель {d} дней.\n\n"
            f"Вопрос: {question.strip()}"
        )

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.6,
        "max_tokens": 800,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"OpenAI API error {resp.status}: {body[:500]}")
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        print(f"OpenAI request failed: {exc}")
        return None
