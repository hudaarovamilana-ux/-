import asyncio

from aiogram import Bot

from database import get_users_for_notification, update_last_notification
from weeks_data import WEEKS_INFO, build_week_message, build_week_message


async def check_week_updates(bot: Bot) -> None:
    """Периодически проверяет пользователей для напоминания о новой неделе."""
    while True:
        try:
            users = get_users_for_notification()
            for user_id, current_week, _last_notification in users:
                try:
                    await bot.send_message(
                        user_id,
                        "🌸 Новая неделя! 🌸\n\n"
                        f"Поздравляю! У тебя началась {current_week} неделя беременности!\n"
                        "👇 Смотри что нового:",
                    )
                    week_data = WEEKS_INFO.get(current_week, {})
                    text = build_week_message(current_week, week_data)

                    await bot.send_message(user_id, text, parse_mode="Markdown")
                    if week_data.get("fact"):
                        await bot.send_message(
                            user_id,
                            f"✨ **Интересный факт:**\n{week_data['fact']}",
                            parse_mode="Markdown",
                        )
                    update_last_notification(user_id, current_week)
                except Exception as exc:  # noqa: BLE001
                    print(f"Ошибка при отправке пользователю {user_id}: {exc}")
            await asyncio.sleep(3600)
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка в планировщике: {exc}")
            await asyncio.sleep(60)
