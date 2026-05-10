"""
Telegram-бот для беременных: long polling + HTTP health для Railway ($PORT).

Переменные окружения:
  BOT_TOKEN — токен от @BotFather (обязательно)
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, timedelta

from flask import Flask

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from pregnancy_content import (
    DISCLAIMER,
    FOOD_NOTE,
    HELP_TEXT,
    WHEN_CALL_DOCTOR,
    hello_message,
    parse_ru_date,
    tip_for_week,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _today() -> date:
    return date.today()


def gestation_week_from_lmp(lmp: date, today: date | None = None) -> int:
    t = today or _today()
    days = (t - lmp).days
    if days < 0:
        return 0
    # Очень грубая оценка: неделя беременности ~ с 1-й недели после LMP
    return days // 7 + 1


def gestation_week_from_due(due: date, today: date | None = None) -> int:
    t = today or _today()
    # Приблизительно: роды ~280 дней от LMP; due ≈ LMP + 280
    lmp_approx = due - timedelta(days=280)
    return gestation_week_from_lmp(lmp_approx, t)


# user_id -> {"week": int | None, "from": str}
_user_meta: dict[int, dict] = {}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        _user_meta.setdefault(update.effective_chat.id, {"week": None, "from": None})
    await update.effective_message.reply_text(hello_message())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_set_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Укажите неделю числом, например: /set_week 18"
        )
        return
    w = int(context.args[0])
    if w < 1 or w > 45:
        await update.effective_message.reply_text("Обычно указывают недели от 1 до 42. Проверьте значение.")
        return
    uid = update.effective_chat.id
    _user_meta[uid] = {"week": w, "from": "manual"}
    await update.effective_message.reply_text(f"Срок сохранён: ~{w} нед. {DISCLAIMER}")


async def cmd_lmp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /lmp ДД.ММ.ГГГГ (пример /lmp 15.06.2025)"
        )
        return
    d = parse_ru_date(" ".join(context.args))
    if d is None:
        await update.effective_message.reply_text("Не удалось разобрать дату. Пример: /lmp 01.06.2025")
        return
    w = gestation_week_from_lmp(d)
    uid = update.effective_chat.id
    _user_meta[uid] = {"week": max(w, 1), "from": "lmp"}
    await update.effective_message.reply_text(
        f"По дате последней менструации ориентир: ~{w} неделя(и). {DISCLAIMER}"
    )


async def cmd_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /due ДД.ММ.ГГГГ (пример /due 08.03.2026)"
        )
        return
    d = parse_ru_date(" ".join(context.args))
    if d is None:
        await update.effective_message.reply_text("Не удалось разобрать дату. Пример: /due 08.03.2026")
        return
    w = gestation_week_from_due(d)
    uid = update.effective_chat.id
    _user_meta[uid] = {"week": max(w, 1), "from": "due"}
    await update.effective_message.reply_text(
        f"По предполагаемой дате родов ориентир: ~{w} неделя(и). {DISCLAIMER}"
    )


def _stored_week(chat_id: int) -> int | None:
    return _user_meta.get(chat_id, {}).get("week")


async def cmd_tip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    week = _stored_week(chat_id)
    if week is None:
        await update.effective_message.reply_text(
            "Сначала укажите срок: /set_week или /lmp или /due"
        )
        return
    await update.effective_message.reply_text(tip_for_week(week) + "\n\n" + DISCLAIMER)


async def cmd_food(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"{FOOD_NOTE}\n\n{DISCLAIMER}")


async def cmd_emergency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"{WHEN_CALL_DOCTOR}\n\n{DISCLAIMER}")


async def cmd_myweek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_chat.id
    meta = _user_meta.get(uid)
    if not meta or meta.get("week") is None:
        await update.effective_message.reply_text("Срок ещё не задан.")
        return
    await update.effective_message.reply_text(
        f"Сохранённый ориентир: ~{meta['week']} нед. ({meta.get('from') or '?'})."
    )


def run_health_server() -> None:
    app = Flask(__name__)

    @app.get("/")
    def health_root():
        return "ok"

    @app.get("/health")
    def health():
        return "ok"

    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


def _read_bot_token() -> str:
    """Токен из окружения: Railway отдаёт его процессу деплоя, имя переменной — строго в верхнем регистре."""
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN"):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw.strip()
    return ""


def main() -> None:
    token = _read_bot_token()
    if not token:
        logger.error(
            "Не найден токен бота. Задайте переменную окружения BOT_TOKEN "
            "(точное имя, без пробелов по краям) для сервиса, где запускается python — "
            "Variables → вкладка сервиса, не только проекта. После сохранения сделайте Redeploy."
        )
        raise SystemExit(1)

    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("set_week", cmd_set_week))
    application.add_handler(CommandHandler("lmp", cmd_lmp))
    application.add_handler(CommandHandler("due", cmd_due))
    application.add_handler(CommandHandler("tip", cmd_tip))
    application.add_handler(CommandHandler("food", cmd_food))
    application.add_handler(CommandHandler("emergency", cmd_emergency))
    application.add_handler(CommandHandler("myweek", cmd_myweek))

    logger.info("Бот запущен (polling + health)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
