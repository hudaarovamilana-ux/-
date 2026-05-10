"""
Точка входа для Railway: health-check на $PORT + aiogram-бот из pregnancy_aiogram_bot.

До импорта бота задаётся BOT_TOKEN из окружения.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from flask import Flask

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN"):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw.strip()
    return ""


def main() -> None:
    token = _read_bot_token()
    if not token:
        logger.error(
            "Не найден токен бота. Задайте BOT_TOKEN у сервиса на Railway и сделайте Redeploy."
        )
        raise SystemExit(1)

    os.environ["BOT_TOKEN"] = token
    threading.Thread(target=run_health_server, daemon=True).start()

    from pregnancy_aiogram_bot import run_bot

    logger.info("Запуск aiogram-бота (polling + health)")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
