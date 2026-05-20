import asyncio
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import (
    get_active_users_for_daily_messaging,
    get_sent_message_indices_today,
    is_message_sent_today,
    mark_message_sent,
    get_users_for_notification,
    update_last_notification,
)
from weeks_data import WEEKS_INFO
from messages import get_random_message


# Создаем планировщик
scheduler = AsyncIOScheduler()


async def send_daily_messages(bot: Bot, schedule_type: str) -> None:
    """Отправляет ежедневные сообщения активным пользователям.
    
    Args:
        bot: экземпляр бота aiogram
        schedule_type: 'morning' (11:00) или 'evening' (21:00)
    """
    try:
        users = get_active_users_for_daily_messaging()
        print(f"📨 Начинаю отправку {schedule_type} сообщений для {len(users)} пользователей")
        
        for user_id, username in users:
            try:
                # Проверяем, было ли уже сообщение отправлено сегодня
                if is_message_sent_today(user_id, schedule_type):
                    print(f"⏭️ Пропускаем пользователя {user_id} - сообщение {schedule_type} уже отправлено")
                    continue
                
                # Получаем индексы сообщений, отправленных сегодня
                sent_indices = get_sent_message_indices_today(user_id)
                
                # Выбираем случайное сообщение, исключая уже отправленные сегодня
                message_index, message_text = get_random_message(exclude_indices=sent_indices)
                
                # Отправляем сообщение
                await bot.send_message(user_id, message_text)
                print(f"✅ Сообщение отправлено пользователю {user_id} (@{username or 'no_username'})")
                
                # Отмечаем сообщение как отправленное
                mark_message_sent(user_id, schedule_type, message_index)
                
                # Небольшая задержка между отправками, чтобы не превысить лимиты Telegram
                await asyncio.sleep(0.5)
                
            except Exception as exc:  # noqa: BLE001
                print(f"❌ Ошибка при отправке {schedule_type} сообщения пользователю {user_id}: {exc}")
        
        print(f"📨 Завершена отправка {schedule_type} сообщений")
        
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Ошибка в функции send_daily_messages: {exc}")


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
                    text = f"🌸 **{current_week} неделя беременности**\n\n"
                    if week_data.get("fruit"):
                        text += f"🍎 Размер плода: {week_data['fruit']}\n\n"
                    if week_data.get("description"):
                        text += f"{week_data['description']}\n\n"
                    if week_data.get("mom_feeling"):
                        text += f"🤰 **Ощущения мамы:**\n{week_data['mom_feeling']}\n\n"

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


def setup_scheduler(bot: Bot) -> None:
    """Настраивает и запускает планировщик для ежедневных сообщений.
    
    Args:
        bot: экземпляр бота aiogram
    """
    # Добавляем задачу на 11:00 (утреннее сообщение)
    scheduler.add_job(
        send_daily_messages,
        trigger='cron',
        hour=11,
        minute=0,
        args=[bot, 'morning'],
        id='morning_messages',
        replace_existing=True
    )
    
    # Добавляем задачу на 21:00 (вечернее сообщение)
    scheduler.add_job(
        send_daily_messages,
        trigger='cron',
        hour=21,
        minute=0,
        args=[bot, 'evening'],
        id='evening_messages',
        replace_existing=True
    )
    
    # Запускаем планировщик
    scheduler.start()
    print("📅 Планировщик ежедневных сообщений запущен (11:00 и 21:00)")
