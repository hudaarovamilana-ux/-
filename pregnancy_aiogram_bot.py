from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from typing import Any, Awaitable, Callable, Dict
from datetime import datetime, timedelta
import asyncio
import html
import logging
import os
import re

# Импортируем данные по неделям
from weeks_data import WEEKS_INFO, build_week_message

# Импортируем функции из database
from database import (
    U_DATE_IN,
    U_DUE,
    U_HEIGHT,
    U_PDAY,
    U_SOURCE,
    U_USERNAME,
    U_WEEK,
    U_WEIGHT,
    U_NOTIF,
    add_kick,
    apply_pregnancy_save,
    clear_pregnancy_onboarding_data,
    count_users,
    ensure_user_exists,
    get_kick_history,
    get_pending_questions,
    get_question_by_id,
    get_today_kicks,
    get_user,
    init_db,
    log_message,
    mark_question_expert_replied,
    refresh_computed_pregnancy,
    save_user_question,
    set_user_awaiting_question,
    start_kick_count,
    update_notifications,
    update_profile_field,
    user_has_complete_onboarding,
    user_is_awaiting_question,
    get_trimester_checklist_statuses,
    set_trimester_checklist_status,
)
from pregnancy_math import (
    approximate_due_from_total_days,
    clamp_manual_week_day,
    format_obstetric_term,
    from_conception,
    from_due_date,
    from_lmp,
    parse_dd_mm_yyyy,
    source_label_ru,
)
from scheduler import check_week_updates
from ai_assistant import generate_pregnancy_answer, is_ai_configured
from trimester_checklist import (
    FIRST_TRIMESTER_DISCLAIMER,
    FIRST_TRIMESTER_ITEM_BY_ID,
    STATUS_LABEL_RU,
    build_first_trimester_keyboard,
    build_first_trimester_status_keyboard,
    build_first_trimester_text,
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logger = logging.getLogger(__name__)


def _parse_expert_chat_ids() -> set[int]:
    """Читает EXPERT_CHAT_IDS: числа через запятую, в т.ч. отрицательные (группы)."""
    raw = os.getenv("EXPERT_CHAT_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Пропущен неверный EXPERT_CHAT_IDS: %r", part)
    return ids


EXPERT_CHAT_IDS: set[int] = _parse_expert_chat_ids()

QUESTION_INTRO_TEXT = (
    "Мы знаем, что во время беременности важно получать ответы быстро, "
    "но при этом хочется слышать живое слово специалиста. Поэтому мы создали гибридный формат:\n\n"
    "• ИИ-помощник ответит вам в течение минуты, чтобы вы могли успокоиться прямо сейчас.\n"
    "• Эксперт получит копию вашего вопроса и обязательно свяжется с вами в этом чате, "
    "как только освободится.\n\n"
    "Не стесняйтесь спрашивать — глупых вопросов не бывает. Мы на связи!\n\n"
    "✍️ Напишите ваш вопрос в этом чате."
)

BOT_PROFILE_DESCRIPTION = (
    "Это виртуальная женская консультация, здесь вы можете узнать всё про свою "
    "беременность, какие анализы сдавать, задать любой интересующий вас вопрос гинекологу🤍\n\n"
    "/start — начать пользоваться ботом\n"
    "/menu — все разделы бота"
)

BOT_MENU_TEXT = (
    "📌 **Меню бота «Женская консультация»**\n\n"
    "Выберите раздел:"
)


def get_menu_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Недели", callback_data="menu_weeks")],
            [InlineKeyboardButton(text="📋 Анализы", callback_data="menu_analyses")],
            [InlineKeyboardButton(text="👶 Подсчёт шевелений", callback_data="menu_kicks")],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile")],
            [InlineKeyboardButton(text="🔔 Уведомления", callback_data="menu_notifications")],
            [InlineKeyboardButton(text="💬 Задать свой вопрос", callback_data="menu_question")],
            [InlineKeyboardButton(text="🏠 Старт", callback_data="menu_start")],
        ]
    )

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

NAV_MSG_IDS = "nav_message_ids"
KEEP_MSG_IDS = "keep_message_ids"


async def _try_delete_message(chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass


async def cleanup_nav_messages(
    chat_id: int,
    state: FSMContext,
    *extra_message_ids: int,
) -> None:
    data = await state.get_data()
    keep_ids = set(data.get(KEEP_MSG_IDS, []))
    ids = list(data.get(NAV_MSG_IDS, []))
    for mid in extra_message_ids:
        if mid and mid not in keep_ids:
            ids.append(mid)
    seen: set[int] = set()
    for mid in ids:
        if mid in seen or mid in keep_ids:
            continue
        seen.add(mid)
        await _try_delete_message(chat_id, mid)
    await state.update_data(**{NAV_MSG_IDS: []})


async def track_nav_message(state: FSMContext, message_id: int) -> None:
    data = await state.get_data()
    ids = list(data.get(NAV_MSG_IDS, []))
    ids.append(message_id)
    await state.update_data(**{NAV_MSG_IDS: ids})


async def track_persistent_message(state: FSMContext, message_id: int) -> None:
    data = await state.get_data()
    ids = list(data.get(KEEP_MSG_IDS, []))
    if message_id not in ids:
        ids.append(message_id)
    await state.update_data(**{KEEP_MSG_IDS: ids})


async def is_persistent_message(state: FSMContext, message_id: int | None) -> bool:
    if not message_id:
        return False
    data = await state.get_data()
    ids = set(data.get(KEEP_MSG_IDS, []))
    return message_id in ids


async def _hide_reply_keyboard(message: Message) -> None:
    # Убираем старую Reply-клавиатуру (если осталась у пользователя).
    marker = await message.answer("…", reply_markup=types.ReplyKeyboardRemove())
    await _try_delete_message(message.chat.id, marker.message_id)


async def send_nav_screen(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    *,
    also_delete: tuple[int, ...] = (),
    keep_message: bool = False,
) -> Message:
    await _hide_reply_keyboard(message)
    await cleanup_nav_messages(message.chat.id, state, *also_delete)
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    if keep_message:
        await track_persistent_message(state, sent.message_id)
    else:
        await track_nav_message(state, sent.message_id)
    return sent


async def send_nav_screen_from_callback(
    callback: CallbackQuery,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    *,
    keep_message: bool = False,
    delete_origin: bool = True,
) -> Message:
    should_delete_origin = delete_origin and not await is_persistent_message(
        state,
        callback.message.message_id if callback.message else None,
    )
    origin_ids = ()
    if should_delete_origin and callback.message:
        origin_ids = (callback.message.message_id,)
    return await send_nav_screen(
        callback.message,
        state,
        text,
        reply_markup,
        parse_mode,
        also_delete=origin_ids,
        keep_message=keep_message,
    )


@dp.startup()
async def _on_startup(bot: Bot, **_kwargs: Any) -> None:
    asyncio.create_task(check_week_updates(bot))
    try:
        await bot.set_my_description(description=BOT_PROFILE_DESCRIPTION)
        await bot.set_my_short_description(
            short_description="Виртуальная женская консультация для беременных 🌸",
        )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Начать пользоваться ботом"),
                BotCommand(command="menu", description="Все функции бота"),
            ]
        )
        logger.info("Профиль бота в Telegram обновлён (описание и команды)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось обновить профиль бота в Telegram: %s", exc)


class MessageLoggingMiddleware(BaseMiddleware):
    """Логирует все входящие сообщения в SQLite."""
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message):
            user = event.from_user
            log_message(
                user_id=user.id if user else None,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                chat_id=event.chat.id if event.chat else None,
                message_text=event.text or "<non-text message>"
            )
        return await handler(event, data)


dp.message.middleware(MessageLoggingMiddleware())

# Состояния FSM
class PregnancyStates(StatesGroup):
    onb_pick_method = State()
    onb_manual_week = State()
    onb_manual_day = State()
    onb_enter_date = State()
    waiting_profile_week = State()
    waiting_profile_height = State()
    waiting_profile_weight = State()
    waiting_profile_name = State()
    start_refill_confirm = State()
    waiting_user_question = State()


def _is_expert(user_id: int | None) -> bool:
    return bool(user_id and user_id in EXPERT_CHAT_IDS)


class AwaitingQuestionFilter(BaseFilter):
    """Вопрос принимаем по FSM или флагу в БД (на случай перезапуска бота)."""

    async def __call__(self, message: Message, state: FSMContext) -> bool:
        uid = message.from_user.id if message.from_user else None
        if uid and _is_expert(uid):
            return False
        text = (message.text or "").strip()
        if text.startswith("/"):
            return False
        current = await state.get_state()
        if current == PregnancyStates.waiting_user_question:
            return True
        return bool(uid and user_is_awaiting_question(uid))


async def _deliver_expert_reply(
    expert_message: types.Message,
    question_id: int,
    reply_text: str,
) -> None:
    if expert_message.from_user:
        await _clear_question_mode_for_user(expert_message.from_user.id)

    row = get_question_by_id(question_id)
    if not row:
        await expert_message.answer(f"Вопрос #{question_id} не найден.")
        return
    if row[8] != "pending_expert":
        await expert_message.answer(f"На вопрос #{question_id} уже дан ответ гинеколога.")
        return

    user_id = row[1]
    try:
        await bot.send_message(
            user_id,
            f"<b>Гинеколог</b>\n\n{html.escape(reply_text)}",
            parse_mode="HTML",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Expert reply to user %s failed: %s", user_id, exc)
        await expert_message.answer(f"Не удалось отправить сообщение пользователю: {exc}")
        return

    mark_question_expert_replied(question_id, reply_text)
    await _clear_question_mode_for_user(user_id)
    await expert_message.answer(f"✅ Ответ на вопрос #{question_id} отправлен пользователю.")


async def _clear_question_mode_for_user(user_id: int) -> None:
    set_user_awaiting_question(user_id, False)


def _format_ai_reply_text(ai_answer: str) -> str:
    return (
        f"<b>Искусственный интеллект</b>\n\n"
        f"{html.escape(ai_answer)}\n\n"
        f"<i>Гинеколог также получила ваш вопрос и ответит в этом чате, "
        f"как только освободится.</i>"
    )


async def _clear_question_mode(state: FSMContext, user_id: int) -> None:
    await state.set_state(None)
    set_user_awaiting_question(user_id, False)


def get_question_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")],
        ]
    )


def get_start_refill_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data="start_refill_yes"),
                InlineKeyboardButton(text="Нет", callback_data="start_refill_no"),
            ]
        ]
    )


def get_onboarding_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Неделя беременности", callback_data="onb_pick_manual")],
            [InlineKeyboardButton(text="Дата последней менструации", callback_data="onb_pick_lmp")],
            [InlineKeyboardButton(text="Дата зачатия", callback_data="onb_pick_conception")],
            [InlineKeyboardButton(text="Предполагаемая дата родов", callback_data="onb_pick_due")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="onb_cancel")],
        ]
    )


def get_manual_week_onboarding_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for w in range(1, 42):
        row.append(InlineKeyboardButton(text=str(w), callback_data=f"onbm_{w}"))
        if len(row) == 7:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="onb_back_pick")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_manual_day_onboarding_keyboard() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=str(d), callback_data=f"onbd_{d}") for d in range(4)]
    row2 = [InlineKeyboardButton(text=str(d), callback_data=f"onbd_{d}") for d in range(4, 7)]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row1,
            row2,
            [InlineKeyboardButton(text="◀️ Назад", callback_data="onb_back_manual_week")],
        ]
    )


def _format_pregnancy_warnings(res) -> str:
    parts = []
    if res.warn_over_42:
        parts.append("⚠️ По расчёту срок больше 42 недель — уточни дату у врача.")
    elif res.warn_over_40:
        parts.append("⚠️ Срок больше 40 недель — скоро встреча с малышом; при сомнениях обратись к врачу.")
    return "\n".join(parts)


async def show_onboarding_pick(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PregnancyStates.onb_pick_method)
    await send_nav_screen(
        message,
        state,
        "Что из этого вы знаете?\n\nВыбери вариант:",
        reply_markup=get_onboarding_method_keyboard(),
    )


async def show_main_menu(message: types.Message, state: FSMContext) -> None:
    await send_nav_screen(
        message,
        state,
        BOT_MENU_TEXT,
        reply_markup=get_menu_inline_keyboard(),
        parse_mode="Markdown",
    )


async def prompt_start_refill(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PregnancyStates.start_refill_confirm)
    await send_nav_screen(
        message,
        state,
        "Вы уже заполнили анкету. Заполнить заново?",
        reply_markup=get_start_refill_keyboard(),
    )


async def handle_start(message: types.Message, state: FSMContext) -> None:
    ensure_user_exists(message.from_user.id)
    await _try_delete_message(message.chat.id, message.message_id)
    user = get_user(message.from_user.id)
    if user_has_complete_onboarding(user):
        await prompt_start_refill(message, state)
        return
    await show_onboarding_pick(message, state)


@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await handle_start(message, state)


@dp.message(Command("menu"))
async def show_bot_menu(message: types.Message, state: FSMContext):
    await _try_delete_message(message.chat.id, message.message_id)
    await state.set_state(None)
    await show_main_menu(message, state)


@dp.callback_query(
    StateFilter(PregnancyStates.start_refill_confirm),
    lambda c: c.data == "start_refill_yes",
)
async def start_refill_yes(callback: types.CallbackQuery, state: FSMContext):
    clear_pregnancy_onboarding_data(callback.from_user.id)
    await show_onboarding_pick(callback.message, state)
    await callback.answer()


@dp.callback_query(
    StateFilter(PregnancyStates.start_refill_confirm),
    lambda c: c.data == "start_refill_no",
)
async def start_refill_no(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await send_nav_screen_from_callback(
        callback,
        state,
        BOT_MENU_TEXT,
        reply_markup=get_menu_inline_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message(PregnancyStates.start_refill_confirm)
async def start_refill_confirm_fallback(message: types.Message):
    await message.answer("Пожалуйста, выбери «Да» или «Нет» кнопками под сообщением.")


@dp.message(lambda message: message.text == "💬 Задать свой вопрос")
async def handle_ask_question_button(message: types.Message, state: FSMContext):
    if _is_expert(message.from_user.id):
        await message.answer(
            "Вы подключены как гинеколог.\n\n"
            "Вопросы пациентов приходят вам автоматически.\n"
            "Чтобы ответить:\n"
            "• нажмите «Ответить» на сообщение с вопросом и напишите текст\n"
            "• или отправьте: <code>/reply 42 ваш текст</code>",
            parse_mode="HTML",
        )
        return
    await state.set_state(PregnancyStates.waiting_user_question)
    set_user_awaiting_question(message.from_user.id, True)
    await send_nav_screen(message, state, QUESTION_INTRO_TEXT, reply_markup=get_question_mode_keyboard())


@dp.callback_query(lambda c: c.data == "question_back_main")
async def question_back_main(callback: types.CallbackQuery, state: FSMContext):
    await _clear_question_mode(state, callback.from_user.id)
    await send_nav_screen_from_callback(
        callback,
        state,
        BOT_MENU_TEXT,
        reply_markup=get_menu_inline_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message(Command("questions"))
async def expert_list_questions(message: types.Message):
    if not _is_expert(message.from_user.id):
        return
    pending = get_pending_questions(limit=15)
    if not pending:
        await message.answer("📭 Нет вопросов, ожидающих ответа гинеколога.")
        return
    lines = ["📋 Вопросы без ответа гинеколога:\n"]
    for row in pending:
        qid, uid, uname, fname, qtext, week, pday, created = row
        short_q = qtext if len(qtext) <= 80 else qtext[:80] + "…"
        term = f"{week}+{pday}д" if week is not None else "?"
        lines.append(f"#{qid} | {fname or uname or uid} | {term} | {short_q}")
    lines.append("\nОтветить: /reply <номер> <текст>")
    await message.answer("\n".join(lines))


@dp.message(Command("reply"))
async def expert_reply_to_user(message: types.Message):
    if not _is_expert(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: /reply <номер_вопроса> <текст ответа>")
        return
    try:
        question_id = int(parts[1])
    except ValueError:
        await message.answer("Номер вопроса должен быть числом.")
        return
    reply_text = parts[2].strip()
    if not reply_text:
        await message.answer("Текст ответа не может быть пустым.")
        return
    await _deliver_expert_reply(message, question_id, reply_text)


@dp.message(F.reply_to_message, F.text)
async def expert_reply_via_telegram_reply(message: types.Message):
    """Гинеколог отвечает через Reply на уведомление о вопросе."""
    if not _is_expert(message.from_user.id):
        return
    if (message.text or "").startswith("/"):
        return

    parent = message.reply_to_message
    parent_text = (parent.text or parent.caption or "") if parent else ""
    match = re.search(r"#(\d+)", parent_text)
    if not match:
        await message.answer(
            "Ответьте (Reply) на сообщение с «Новый вопрос #…» "
            "или используйте: /reply <номер> <текст>"
        )
        return

    question_id = int(match.group(1))
    await _deliver_expert_reply(message, question_id, message.text.strip())


@dp.message(AwaitingQuestionFilter())
async def process_user_question(message: types.Message, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_user_question)
    set_user_awaiting_question(message.from_user.id, True)

    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Пожалуйста, напишите вопрос текстом.",
            reply_markup=get_question_mode_keyboard(),
        )
        return

    if text in ("/menu", "меню", "Меню"):
        await _clear_question_mode(state, message.from_user.id)
        await show_main_menu(message, state)
        return

    user = message.from_user
    uid = user.id
    ensure_user_exists(uid)
    refresh_computed_pregnancy(uid)
    db_user = get_user(uid)
    week = db_user[U_WEEK] if db_user else None
    pday = db_user[U_PDAY] if db_user and db_user[U_PDAY] is not None else 0

    wait_msg = await message.answer("⏳ ИИ-помощник готовит ответ…")
    logger.info("AI question from user %s: %s", uid, text[:120])

    ai_answer = await generate_pregnancy_answer(text, week, pday)
    if ai_answer:
        logger.info("AI answer ready for user %s (%d chars)", uid, len(ai_answer))
    else:
        logger.warning("AI answer missing for user %s (check DEEPSEEK_API_KEY / API logs)", uid)

    question_id = save_user_question(
        user_id=uid,
        username=user.username,
        full_name=user.full_name,
        question_text=text,
        ai_answer=ai_answer,
        pregnancy_week=week,
        pregnancy_day=pday,
    )

    await notify_experts_about_question(
        question_id=question_id,
        user=user,
        question_text=text,
        ai_answer=ai_answer,
        week=week,
        pday=pday,
    )

    if ai_answer:
        try:
            await wait_msg.edit_text(
                _format_ai_reply_text(ai_answer),
                parse_mode="HTML",
                reply_markup=get_question_mode_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to edit AI reply (HTML), fallback to plain text: %s", exc)
            await wait_msg.edit_text(
                f"Искусственный интеллект\n\n{ai_answer}",
                reply_markup=get_question_mode_keyboard(),
            )
    else:
        await wait_msg.edit_text(
            "✅ Ваш вопрос сохранён.\n\n"
            "ИИ-помощник сейчас недоступен, но гинеколог получила ваш вопрос "
            "и обязательно ответит в этом чате.",
            reply_markup=get_question_mode_keyboard(),
        )


async def notify_experts_about_question(
    question_id: int,
    user: types.User,
    question_text: str,
    ai_answer: str | None,
    week: int | None,
    pday: int,
) -> None:
    if not EXPERT_CHAT_IDS:
        logger.warning(
            "EXPERT_CHAT_IDS не задан — вопрос #%s не отправлен гинекологу",
            question_id,
        )
        return

    username = f"@{user.username}" if user.username else "без username"
    term = "не указан"
    if week is not None:
        term = f"{week} нед. {pday} дн. (акушерский)"

    expert_text = (
        f"<b>🆕 Новый вопрос #{question_id}</b>\n\n"
        f"👤 {html.escape(user.full_name or '—')} ({html.escape(username)})\n"
        f"🆔 user_id: <code>{user.id}</code>\n"
        f"🤰 Срок: {html.escape(term)}\n\n"
        f"<b>❓ Вопрос:</b>\n{html.escape(question_text)}"
    )
    if ai_answer:
        preview = ai_answer if len(ai_answer) <= 500 else ai_answer[:500] + "…"
        expert_text += f"\n\n<b>🤖 Ответ ИИ:</b>\n{html.escape(preview)}"
    expert_text += (
        f"\n\nОтветить пользователю:\n"
        f"<code>/reply {question_id} ваш текст</code>"
    )

    for expert_id in EXPERT_CHAT_IDS:
        try:
            await bot.send_message(expert_id, expert_text, parse_mode="HTML")
            logger.info("Вопрос #%s отправлен гинекологу %s", question_id, expert_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Не удалось отправить вопрос #%s гинекологу %s: %s",
                question_id,
                expert_id,
                exc,
            )
            plain = (
                f"Новый вопрос #{question_id}\n\n"
                f"От: {user.full_name} ({username}), id {user.id}\n"
                f"Срок: {term}\n\n"
                f"Вопрос:\n{question_text}\n\n"
                f"Ответить: /reply {question_id} ваш текст"
            )
            try:
                await bot.send_message(expert_id, plain)
                logger.info("Вопрос #%s отправлен гинекологу %s (plain text)", question_id, expert_id)
            except Exception as exc2:  # noqa: BLE001
                logger.error(
                    "Повторная отправка вопроса #%s гинекологу %s не удалась: %s",
                    question_id,
                    expert_id,
                    exc2,
                )


@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    """Помогает узнать Telegram ID для настройки EXPERT_CHAT_IDS."""
    uid = message.from_user.id
    if uid in EXPERT_CHAT_IDS:
        extra = "\n\n✅ Этот ID есть в EXPERT_CHAT_IDS — сюда должны приходить вопросы."
    else:
        extra = (
            "\n\nℹ️ Чтобы получать вопросы пациентов, добавьте этот ID "
            "в переменную EXPERT_CHAT_IDS на Railway и перезапустите бота."
        )
    await message.answer(f"Ваш Telegram ID: <code>{uid}</code>{extra}", parse_mode="HTML")


@dp.message(Command("stats"))
async def stats(message: types.Message):
    total = count_users()
    await message.answer(f"📊 Всего пользователей: {total}")


def _build_week_view_text(week: int, header: str = "") -> str:
    week_data = get_week_info(week)
    body = build_week_message(week, week_data)
    if week_data.get("fact"):
        body += f"\n\n✨ **Интересный факт:**\n{week_data['fact']}"
    if header:
        return f"{header}\n\n{body}"
    return body


def _build_week_view_keyboard(week: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if week <= 12:
        rows.append(
            [InlineKeyboardButton(text="📋 Анализы 1-го триместра", callback_data="first_trimester_analyses")]
        )
    elif week <= 27:
        rows.append(
            [InlineKeyboardButton(text="📋 Анализы 2-го триместра", callback_data="second_trimester_analyses")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="📋 Анализы 3-го триместра", callback_data="third_trimester_analyses")]
        )
    rows.append([InlineKeyboardButton(text="📌 Меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def finish_onboarding_screen(
    message: Message,
    state: FSMContext,
    week: int,
    header: str = "",
    *,
    user_message_id: int | None = None,
    callback: CallbackQuery | None = None,
) -> None:
    text = _build_week_view_text(week, header)
    keyboard = _build_week_view_keyboard(week)
    also_delete: tuple[int, ...] = ()
    if user_message_id:
        also_delete = (user_message_id,)
    if callback:
        await send_nav_screen(
            message,
            state,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
            also_delete=(*also_delete, callback.message.message_id),
            keep_message=True,
        )
    else:
        await send_nav_screen(
            message,
            state,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
            also_delete=also_delete,
            keep_message=True,
        )
    await state.set_state(None)


@dp.callback_query(lambda c: c.data == "onb_cancel")
async def onb_cancel(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user_has_complete_onboarding(user):
        await state.clear()
        await send_nav_screen_from_callback(
            callback,
            state,
            BOT_MENU_TEXT,
            reply_markup=get_menu_inline_keyboard(),
            parse_mode="Markdown",
        )
    else:
        await send_nav_screen_from_callback(
            callback,
            state,
            "Для начала работы нажми /start",
        )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "onb_back_pick")
async def onb_back_pick(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.onb_pick_method)
    await send_nav_screen_from_callback(
        callback,
        state,
        "Что из этого вы знаете?\n\nВыбери вариант:",
        reply_markup=get_onboarding_method_keyboard(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "onb_pick_manual")
async def onb_pick_manual(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.onb_manual_week)
    await send_nav_screen_from_callback(
        callback,
        state,
        "Выбери неделю беременности (1–41):",
        reply_markup=get_manual_week_onboarding_keyboard(),
    )
    await callback.answer()


@dp.callback_query(StateFilter(PregnancyStates.onb_manual_week), lambda c: c.data.startswith("onbm_"))
async def onb_manual_week_chosen(callback: types.CallbackQuery, state: FSMContext):
    week = int(callback.data.split("_")[1])
    await state.update_data(onb_manual_week=week)
    await state.set_state(PregnancyStates.onb_manual_day)
    await send_nav_screen_from_callback(
        callback,
        state,
        f"Неделя: {week}. Теперь выбери день внутри недели (0–6):",
        reply_markup=get_manual_day_onboarding_keyboard(),
    )
    await callback.answer()


@dp.callback_query(StateFilter(PregnancyStates.onb_manual_day), lambda c: c.data == "onb_back_manual_week")
async def onb_back_manual_week(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.onb_manual_week)
    await send_nav_screen_from_callback(
        callback,
        state,
        "Выбери неделю беременности (1–41):",
        reply_markup=get_manual_week_onboarding_keyboard(),
    )
    await callback.answer()


@dp.callback_query(StateFilter(PregnancyStates.onb_manual_day), lambda c: c.data.startswith("onbd_"))
async def onb_manual_day_chosen(callback: types.CallbackQuery, state: FSMContext):
    day = int(callback.data.split("_")[1])
    data = await state.get_data()
    week_raw = int(data.get("onb_manual_week", 1))
    w, d = clamp_manual_week_day(week_raw, day)
    total_days = w * 7 + d
    due_iso = approximate_due_from_total_days(total_days)
    apply_pregnancy_save(
        callback.from_user.id,
        week=w,
        pregnancy_day=d,
        source="manual",
        date_input=None,
        due_date=due_iso,
        last_period_date=None,
    )
    await state.clear()
    extra = ""
    if week_raw > 40 or (week_raw == 40 and d > 0):
        extra = "\n⚠️ Срок больше 40 недель — уточни у врача."
    if week_raw > 42:
        extra += "\n⚠️ Срок больше 42 недель — проверь введённые данные."
    header = f"Ваша беременность:\n{format_obstetric_term(w, d)}{extra}"
    info_week = max(1, min(w, 41))
    await finish_onboarding_screen(
        callback.message,
        state,
        info_week,
        header,
        callback=callback,
    )
    await callback.answer()


async def _prompt_date_input(callback: types.CallbackQuery, state: FSMContext, kind: str, prompt: str) -> None:
    await state.set_state(PregnancyStates.onb_enter_date)
    await state.update_data(onb_anchor=kind)
    await send_nav_screen_from_callback(callback, state, prompt, parse_mode="Markdown")


@dp.callback_query(lambda c: c.data == "onb_pick_lmp")
async def onb_pick_lmp(callback: types.CallbackQuery, state: FSMContext):
    await _prompt_date_input(
        callback,
        state,
        "lmp",
        "Введите дату последней менструации в формате **ДД.ММ.ГГГГ** (например, 15.06.2025):",
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "onb_pick_conception")
async def onb_pick_conception(callback: types.CallbackQuery, state: FSMContext):
    await _prompt_date_input(
        callback,
        state,
        "conception",
        "Введите дату зачатия в формате **ДД.ММ.ГГГГ**:",
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "onb_pick_due")
async def onb_pick_due(callback: types.CallbackQuery, state: FSMContext):
    await _prompt_date_input(
        callback,
        state,
        "due_date",
        "Введите предполагаемую дату родов в формате **ДД.ММ.ГГГГ**:",
    )
    await callback.answer()


@dp.message(PregnancyStates.onb_enter_date)
async def onb_process_date_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("onb_anchor")
    raw = (message.text or "").strip()
    d = parse_dd_mm_yyyy(raw)
    if d is None:
        await message.answer("❌ Неверный формат. Введите дату как **ДД.ММ.ГГГГ** (например, 01.06.2025).")
        return
    today = datetime.now().date()
    date_iso = d.strftime("%Y-%m-%d")

    if kind == "lmp":
        res = from_lmp(d, today)
        due_iso = (d + timedelta(days=280)).strftime("%Y-%m-%d")
        lmp_iso = date_iso
    elif kind == "conception":
        res = from_conception(d, today)
        due_iso = (d + timedelta(days=266)).strftime("%Y-%m-%d")
        lmp_iso = None
    elif kind == "due_date":
        res = from_due_date(d, today)
        due_iso = date_iso
        lmp_iso = None
    else:
        await message.answer("❌ Неизвестный шаг. Нажми /start.")
        await state.clear()
        return

    if res.error:
        await message.answer(f"❌ {res.error}")
        return

    apply_pregnancy_save(
        message.from_user.id,
        week=res.week,
        pregnancy_day=res.day,
        source=kind,
        date_input=date_iso,
        due_date=due_iso,
        last_period_date=lmp_iso,
    )
    await state.clear()
    warn = _format_pregnancy_warnings(res)
    header = (
        f"Ваша беременность:\n{format_obstetric_term(res.week, res.day)}"
        + (f"\n{warn}" if warn else "")
    )
    info_week = max(1, min(res.week, 41))
    await finish_onboarding_screen(
        message,
        state,
        info_week,
        header,
        user_message_id=message.message_id,
    )


@dp.message(PregnancyStates.onb_pick_method)
async def onb_pick_method_fallback(message: types.Message):
    await message.answer("Пожалуйста, выбери вариант кнопками под предыдущим сообщением.")


@dp.message(PregnancyStates.onb_manual_week)
async def onb_manual_week_text_fallback(message: types.Message):
    await message.answer("Выбери неделю кнопками с числами (1–41).")


@dp.message(PregnancyStates.onb_manual_day)
async def onb_manual_day_text_fallback(message: types.Message):
    await message.answer("Выбери день кнопками 0–6.")


async def show_weeks_menu(message: types.Message, state: FSMContext) -> None:
    await send_nav_screen(
        message,
        state,
        "🌸 Выбери неделю беременности:",
        reply_markup=get_all_weeks_keyboard(),
    )


async def show_analyses_menu(message: types.Message, state: FSMContext) -> None:
    await send_nav_screen(
        message,
        state,
        "📋 Выбери триместр:",
        reply_markup=get_analyses_menu_keyboard(),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("menu_"))
async def menu_inline_action(callback: types.CallbackQuery, state: FSMContext):
    """Кликабельные разделы в /menu."""
    msg = callback.message
    action = callback.data.removeprefix("menu_")

    if action == "weeks":
        await send_nav_screen_from_callback(
            callback,
            state,
            "🌸 Выбери неделю беременности:",
            reply_markup=get_all_weeks_keyboard(),
        )
    elif action == "analyses":
        await send_nav_screen_from_callback(
            callback,
            state,
            "📋 Выбери триместр:",
            reply_markup=get_analyses_menu_keyboard(),
        )
    elif action == "kicks":
        await kick_counter_menu(msg, state, callback=callback)
    elif action == "profile":
        await state.set_state(None)
        await send_profile_message(msg, state, callback=callback)
    elif action == "notifications":
        await notifications_settings(msg, state, callback=callback)
    elif action == "question":
        await handle_ask_question_button(msg, state)
        await _try_delete_message(msg.chat.id, callback.message.message_id)
    elif action == "start":
        await handle_start(msg, state)
        await _try_delete_message(msg.chat.id, callback.message.message_id)
    else:
        await callback.answer("Неизвестный раздел")
        return

    await callback.answer()


def _format_profile_value(value: Any) -> str:
    if value is None:
        return "не указано"
    text = str(value).strip()
    return text if text else "не указано"


def get_profile_actions_keyboard(missing_height_or_weight: bool) -> InlineKeyboardMarkup:
    buttons = []
    if missing_height_or_weight:
        buttons.append(
            [InlineKeyboardButton(text="👉 Добавить информацию", callback_data="profile_add_info")]
        )
    buttons.append([InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_profile_add_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Неделя", callback_data="profile_edit_week")],
            [InlineKeyboardButton(text="Рост", callback_data="profile_edit_height")],
            [InlineKeyboardButton(text="Вес", callback_data="profile_edit_weight")],
            [InlineKeyboardButton(text="Имя", callback_data="profile_edit_name")],
            [InlineKeyboardButton(text="Назад", callback_data="profile_show")],
        ]
    )


async def send_profile_message(
    message: types.Message,
    state: FSMContext,
    *,
    callback: CallbackQuery | None = None,
) -> None:
    uid = message.from_user.id
    refresh_computed_pregnancy(uid)
    ensure_user_exists(uid)
    user = get_user(uid)

    week_num = user[U_WEEK] if user else None
    day_num = user[U_PDAY] if user and user[U_PDAY] is not None else 0
    src = user[U_SOURCE] if user else None

    if week_num is None:
        preg_line = "не указано"
    else:
        preg_line = f"акушерский срок — {week_num} неделя {day_num} день"

    source_line = source_label_ru(src) if src else ("ранее сохранённый срок" if week_num is not None else "не указано")

    due_date_raw = user[U_DUE] if user else None
    height = _format_profile_value(user[U_HEIGHT] if user else None)
    weight = _format_profile_value(user[U_WEIGHT] if user else None)
    stored_name = user[U_USERNAME] if user else None
    username = stored_name or message.from_user.username or message.from_user.full_name
    name_text = _format_profile_value(username)

    due_date_text = "не указано"
    if due_date_raw:
        try:
            due_date_text = datetime.strptime(due_date_raw, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            due_date_text = due_date_raw

    warn_extra = ""
    if src in ("lmp", "conception", "due_date") and user and user[U_DATE_IN]:
        raw_di = user[U_DATE_IN]
        if "." in str(raw_di):
            d_anchor = parse_dd_mm_yyyy(str(raw_di))
        else:
            try:
                d_anchor = datetime.strptime(str(raw_di), "%Y-%m-%d").date()
            except ValueError:
                d_anchor = None
        if d_anchor:
            today = datetime.now().date()
            if src == "lmp":
                res = from_lmp(d_anchor, today)
            elif src == "conception":
                res = from_conception(d_anchor, today)
            else:
                res = from_due_date(d_anchor, today)
            wtxt = _format_pregnancy_warnings(res)
            if wtxt:
                warn_extra = "\n" + wtxt

    profile_text = (
        "👤 **Профиль:**\n\n"
        f"Неделя беременности: {preg_line}\n"
        f"Источник: {source_line}\n\n"
        f"Имя: {name_text}\n"
        f"Рост: {height}\n"
        f"Вес: {weight}\n"
        f"Предполагаемая дата родов: {due_date_text}"
        f"{warn_extra}"
    )
    keyboard = get_profile_actions_keyboard(
        missing_height_or_weight=(user is None or user[U_HEIGHT] is None or user[U_WEIGHT] is None)
    )
    if callback:
        await send_nav_screen_from_callback(
            callback,
            state,
            profile_text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    else:
        await send_nav_screen(
            message,
            state,
            profile_text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )


@dp.callback_query(lambda c: c.data == "profile_show")
async def profile_show_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await send_profile_message(callback.message, state, callback=callback)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_add_info")
async def profile_add_info_callback(callback: types.CallbackQuery, state: FSMContext):
    await send_nav_screen_from_callback(
        callback,
        state,
        "📝 Что хотите добавить или изменить?",
        reply_markup=get_profile_add_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_week")
async def profile_edit_week_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_week)
    await send_nav_screen_from_callback(
        callback, state, "Введите неделю беременности (1-42):"
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_height")
async def profile_edit_height_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_height)
    await send_nav_screen_from_callback(callback, state, "Введите ваш рост (в см):")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_weight")
async def profile_edit_weight_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_weight)
    await send_nav_screen_from_callback(callback, state, "Введите ваш вес (в кг):")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_name")
async def profile_edit_name_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_name)
    await send_nav_screen_from_callback(callback, state, "Введите имя:")
    await callback.answer()


@dp.message(PregnancyStates.waiting_profile_week)
async def save_profile_week(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Неделя должна быть числом от 1 до 42.")
        return
    week = int(text)
    if week < 1 or week > 42:
        await message.answer("Неделя должна быть в диапазоне 1-42.")
        return
    update_profile_field(message.from_user.id, "week", week)
    update_profile_field(message.from_user.id, "pregnancy_day", 0)
    update_profile_field(message.from_user.id, "source", "manual")
    update_profile_field(message.from_user.id, "date_input", None)
    await state.clear()
    await _try_delete_message(message.chat.id, message.message_id)
    await send_profile_message(message, state)


@dp.message(PregnancyStates.waiting_profile_height)
async def save_profile_height(message: types.Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    try:
        height = int(float(text))
    except ValueError:
        await message.answer("Рост должен быть числом, например 172.")
        return
    if height < 100 or height > 250:
        await message.answer("Укажите рост в см (обычно 100-250).")
        return
    update_profile_field(message.from_user.id, "height_cm", height)
    await state.clear()
    await _try_delete_message(message.chat.id, message.message_id)
    await send_profile_message(message, state)


@dp.message(PregnancyStates.waiting_profile_weight)
async def save_profile_weight(message: types.Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    try:
        weight = round(float(text), 1)
    except ValueError:
        await message.answer("Вес должен быть числом, например 65.5.")
        return
    if weight < 30 or weight > 250:
        await message.answer("Укажите корректный вес в кг.")
        return
    update_profile_field(message.from_user.id, "weight_kg", weight)
    await state.clear()
    await _try_delete_message(message.chat.id, message.message_id)
    await send_profile_message(message, state)


@dp.message(PregnancyStates.waiting_profile_name)
async def save_profile_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым.")
        return
    update_profile_field(message.from_user.id, "username", name[:64])
    await state.clear()
    await _try_delete_message(message.chat.id, message.message_id)
    await send_profile_message(message, state)


@dp.callback_query(lambda c: c.data.startswith("week_"))
async def show_week_info_from_menu(callback: types.CallbackQuery, state: FSMContext):
    """Показывает информацию о выбранной неделе."""
    week = int(callback.data.split("_")[1])
    text = _build_week_view_text(week)
    keyboard = _build_week_view_keyboard(week)
    keyboard.inline_keyboard.insert(
        0,
        [InlineKeyboardButton(text="◀️ К неделям", callback_data="back_to_weeks")],
    )
    await send_nav_screen_from_callback(
        callback,
        state,
        text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "back_to_weeks")
async def back_to_weeks(callback: types.CallbackQuery, state: FSMContext):
    await send_nav_screen_from_callback(
        callback,
        state,
        "🌸 Выбери неделю беременности:",
        reply_markup=get_all_weeks_keyboard(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_main_menu(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if user_is_awaiting_question(uid):
        await _clear_question_mode(state, uid)
    await state.set_state(None)
    await send_nav_screen_from_callback(
        callback,
        state,
        BOT_MENU_TEXT,
        reply_markup=get_menu_inline_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()
@dp.callback_query(lambda c: c.data.startswith("analyses_"))
async def show_analyses_by_trimester(callback: types.CallbackQuery, state: FSMContext):
    trimester = int(callback.data.split("_")[1])
    persistent_origin = await is_persistent_message(state, callback.message.message_id)

    if trimester == 1:
        await send_first_trimester_checklist(
            callback.message,
            callback.from_user.id,
            state,
            callback=callback,
            keep_message=persistent_origin,
        )
        await callback.answer()
        return

    if trimester == 2:
        text = SECOND_TRIMESTER_ANALYSES
    else:
        text = THIRD_TRIMESTER_ANALYSES

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К триместрам", callback_data="menu_analyses")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")],
        ]
    )
    await send_nav_screen_from_callback(
        callback,
        state,
        text,
        reply_markup=keyboard,
        parse_mode="Markdown",
        keep_message=persistent_origin,
    )
    await callback.answer()
@dp.callback_query(lambda c: c.data in ["notif_on", "notif_off"])
async def set_notifications(callback: types.CallbackQuery, state: FSMContext):
    enabled = 1 if callback.data == "notif_on" else 0
    update_notifications(callback.from_user.id, enabled)
    await notifications_settings(callback.message, state, callback=callback)
    status = "включены" if enabled else "выключены"
    await callback.answer(f"Уведомления {status}")


async def notifications_settings(
    message: types.Message,
    state: FSMContext,
    *,
    callback: CallbackQuery | None = None,
) -> None:
    """Настройка уведомлений."""
    try:
        user = get_user(message.from_user.id)

        if not user:
            text = "❌ Сначала введи свою неделю беременности через /start!"
            if callback:
                await send_nav_screen_from_callback(callback, state, text)
            else:
                await send_nav_screen(message, state, text)
            return

        notifications_enabled = user[U_NOTIF]
        status = "✅ Включены" if notifications_enabled == 1 else "❌ Выключены"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Включить", callback_data="notif_on")],
                [InlineKeyboardButton(text="❌ Выключить", callback_data="notif_off")],
                [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")],
            ]
        )

        text = (
            f"🔔 **Настройки уведомлений**\n\n"
            f"Статус: {status}\n"
            f"Твоя неделя: {user[U_WEEK]}\n\n"
            f"Я буду напоминать тебе о новой неделе каждые 7 дней!"
        )

        if callback:
            await send_nav_screen_from_callback(
                callback, state, text, reply_markup=keyboard, parse_mode="Markdown"
            )
        else:
            await send_nav_screen(
                message, state, text, reply_markup=keyboard, parse_mode="Markdown"
            )

    except Exception as e:
        err = f"❌ Ошибка: {e}"
        if callback:
            await send_nav_screen_from_callback(callback, state, err)
        else:
            await send_nav_screen(message, state, err)
        print(f"❌ Ошибка в notifications_settings: {e}")


def _kick_counter_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Малыш пошевелился", callback_data="add_kick")],
            [InlineKeyboardButton(text="📈 Итог за 2 часа", callback_data="check_2h")],
            [InlineKeyboardButton(text="📅 История", callback_data="kick_history")],
            [InlineKeyboardButton(text="ℹ️ О норме", callback_data="kick_info")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")],
        ]
    )


async def kick_counter_menu(
    message: types.Message,
    state: FSMContext,
    *,
    callback: CallbackQuery | None = None,
) -> None:
    """Меню подсчета шевелений."""
    refresh_computed_pregnancy(message.from_user.id)
    user = get_user(message.from_user.id)

    if not user or user[U_WEEK] is None:
        text = "❌ Сначала укажи срок через /start!"
        if callback:
            await send_nav_screen_from_callback(callback, state, text)
        else:
            await send_nav_screen(message, state, text)
        return

    current_week = user[U_WEEK]
    start_kick_count(message.from_user.id)
    today_kicks = get_today_kicks(message.from_user.id)

    intro = ""
    if current_week < 28:
        intro = (
            f"🌸 У тебя сейчас {current_week} неделя.\n"
            f"Обычно шевеления хорошо ощущаются с 28 недели, "
            f"но ты уже можешь практиковаться!\n\n"
        )

    text = (
        f"{intro}"
        f"👶 **Подсчет шевелений**\n\n"
        f"Нажимай кнопку каждый раз,\n"
        f"когда чувствуешь движение малыша 🤍\n\n"
        f"📊 Сегодня: {today_kicks} раз"
    )

    if callback:
        await send_nav_screen_from_callback(
            callback,
            state,
            text,
            reply_markup=_kick_counter_keyboard(),
            parse_mode="Markdown",
        )
    else:
        await send_nav_screen(
            message,
            state,
            text,
            reply_markup=_kick_counter_keyboard(),
            parse_mode="Markdown",
        )

@dp.callback_query(lambda c: c.data == "add_kick")
async def add_kick_callback(callback: types.CallbackQuery):
    """Добавляет одно шевеление"""
    new_count = add_kick(callback.from_user.id)
    
    # Получаем информацию о пользователе
    # Оцениваем активность
    if new_count >= 10:
        message_text = f"**+1**\n\n📊 Всего за сегодня: **{new_count} раз** 🤍\n\n✨ Это хорошая активность!"
    else:
        message_text = f"**+1**\n\n📊 Всего за сегодня: **{new_count} раз**\n\n💭 Активность ниже обычной"
    
    # Обновляем сообщение (редактируем)
    await callback.message.edit_text(
        f"👶 **Подсчет шевелений**\n\n"
        f"{message_text}",
        reply_markup=_kick_counter_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer("✅ Засчитано!")

@dp.callback_query(lambda c: c.data == "check_2h")
async def check_2h_kicks(callback: types.CallbackQuery):
    """Проверка шевелений за последние 2 часа"""
    today_kicks = get_today_kicks(callback.from_user.id)
    
    text = (
        f"📈 **Анализ шевелений**\n\n"
        f"За сегодня: {today_kicks} раз\n\n"
        f"**Норма:** минимум 10 движений за 2 часа\n\n"
    )
    
    if today_kicks >= 10:
        text += "✅ Отличная активность! Малыш хорошо двигается 🤍"
    else:
        text += (
            "⚠️ Активность ниже обычной.\n\n"
            "💡 Попробуй:\n"
            "• немного поесть\n"
            "• выпить воды\n"
            "• лечь на левый бок\n"
            "• спокойно полежать"
        )
    
    await callback.message.edit_text(text, reply_markup=_kick_counter_keyboard(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "kick_history")
async def show_kick_history(callback: types.CallbackQuery):
    """Показывает историю шевелений"""
    from database import get_kick_history
    
    history = get_kick_history(callback.from_user.id, days=7)
    
    if not history:
        await callback.message.edit_text(
            "📅 Пока нет данных. Начни подсчет сегодня!",
            reply_markup=_kick_counter_keyboard(),
        )
        await callback.answer()
        return
    
    text = "📅 **История шевелений за 7 дней**\n\n"
    for date, count in history:
        # Форматируем дату
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d.%m")
        text += f"• {formatted_date}: {count} раз\n"
    
    await callback.message.edit_text(text, reply_markup=_kick_counter_keyboard(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "kick_info")
async def show_kick_info(callback: types.CallbackQuery):
    """Показывает информацию о норме шевелений"""
    text = (
        "ℹ️ **О шевелениях малыша**\n\n"
        "**Норма:**\n"
        "Минимум 10 движений за 2 часа, когда мама спокойно лежит или сидит.\n\n"
        "**📈 Когда малыш чаще шевелится:**\n"
        "• вечером\n"
        "• после еды\n"
        "• когда мама отдыхает\n\n"
        "**⚠️ Когда к врачу:**\n"
        "• полное отсутствие движений более 3–4 часов\n"
        "• существенное уменьшение шевелений\n"
        "• резкие, хаотичные движения\n\n"
        "**💛 Помни:**\n"
        "У малыша есть периоды сна (20–40 минут).\n"
        "Если кажется, что он мало двигается — попробуй поесть, выпить воды, прилечь на левый бок."
    )
    
    await callback.message.edit_text(text, reply_markup=_kick_counter_keyboard(), parse_mode="Markdown")
    await callback.answer()
def get_all_weeks_keyboard():
    """Создает клавиатуру со всеми неделями (1-41)"""
    buttons = []
    row = []
    for week in range(1, 42):
        row.append(InlineKeyboardButton(text=str(week), callback_data=f"week_{week}"))
        if len(row) == 5:  # по 5 кнопок в ряду
            buttons.append(row)
            row = []
    if row:  # Добавляем оставшиеся кнопки
        buttons.append(row)
    
    # Добавляем кнопку "Назад"
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard

# Меню выбора триместра для анализов
def get_analyses_menu_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌸 1 триместр", callback_data="analyses_1")],
            [InlineKeyboardButton(text="🌿 2 триместр", callback_data="analyses_2")],
            [InlineKeyboardButton(text="🍂 3 триместр", callback_data="analyses_3")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")],
        ]
    )
    return keyboard


def get_week_info(week):
    """Возвращает информацию о неделе из отдельного файла"""
    return WEEKS_INFO.get(week, {
        'fruit': '🍊 апельсин',
        'description': 'Твой малыш активно растет и развивается!',
        'mom_feeling': 'Прислушивайся к своему организму и отдыхай',
        'nutrition': 'Питайся разнообразно и пей достаточно воды',
        'doctors': 'Регулярно посещай своего врача',
        'fact': ''
    })

def calculate_current_week(registered_date, initial_week):
    """Рассчитывает текущую неделю на основе даты регистрации"""
    if isinstance(registered_date, str):
        registered_date = datetime.strptime(registered_date, "%Y-%m-%d %H:%M:%S")
    
    days_passed = (datetime.now() - registered_date).days
    weeks_passed = days_passed // 7
    current_week = initial_week + weeks_passed
    
    return min(current_week, 42)  # Не больше 42 недель

# Информация об анализах второго триместра
SECOND_TRIMESTER_ANALYSES = """
📋 АНАЛИЗЫ ВТОРОГО ТРИМЕСТРА (13–27 недель)

🌸 Это самый спокойный период, но расслабляться рано!

━━━━━━━━━━━━━━━━━━━━━━━
🩺 16–20 НЕДЕЛЬ (ОЧЕНЬ ВАЖНО!)
━━━━━━━━━━━━━━━━━━━━━━━

🔬 ВТОРОЙ СКРИНИНГ (тройной тест):
   • АФП (альфа-фетопротеин)
   • ХГЧ (хорионический гонадотропин)
   • Эстриол (свободный эстриол)

🎯 Зачем: Исключить пороки развития нервной трубки, синдром Дауна и другие хромосомные аномалии.

📊 УЗИ 2-го триместра (18–21 неделя):
   • Оценка всех органов малыша
   • Можно узнать пол! 👶
   • Проверка плаценты и пуповины
   • Количество околоплодных вод

━━━━━━━━━━━━━━━━━━━━━━━
🩸 24–28 НЕДЕЛЬ
━━━━━━━━━━━━━━━━━━━━━━━

🍬 Глюкозотолерантный тест (ГТТ):
   • Проверка на гестационный диабет
   • Пьёшь сладкую воду, забирают кровь 3 раза
   • НЕЛЬЗЯ есть за 8-10 часов до теста!

⚠️ ВАЖНО: Если у тебя был диабет до беременности или крупный плод — тест могут назначить раньше!

━━━━━━━━━━━━━━━━━━━━━━━
🩺 НА КАЖДОМ ПРИЁМЕ (каждые 3-4 недели)
━━━━━━━━━━━━━━━━━━━━━━━

✅ Обязательно:
   • Общий анализ мочи (белок, лейкоциты)
   • Измерение давления (отёки? давление?)
   • Взвешивание (контроль набора веса)
   • Высота дна матки (как растёт малыш)
   • Окружность живота
   • Прослушивание сердцебиения малыша

━━━━━━━━━━━━━━━━━━━━━━━
💉 ПО НАЗНАЧЕНИЮ:
━━━━━━━━━━━━━━━━━━━━━━━

🔹 Общий анализ крови — проверка гемоглобина (анемия частая!)
🔹 Анализ на резус-конфликт — если у мамы резус-отрицательная кровь
🔹 ТТГ — гормоны щитовидной железы
🔹 Мазок на флору — исключить инфекции
🔹 Коагулограмма — свёртываемость крови
🔹 Анализ на TORCH-инфекции (по назначению)

━━━━━━━━━━━━━━━━━━━━━━━
👩‍⚕️ КАКИХ ВРАЧЕЙ ПОСЕТИТЬ:
━━━━━━━━━━━━━━━━━━━━━━━

✅ Акушер-гинеколог — каждые 3-4 недели
✅ Стоматолог — обязательно! (лечить зубы можно и нужно)
✅ Терапевт — 1 раз во 2-м триместре
✅ Окулист — при проблемах со зрением
✅ ЛОР — при хронических заболеваниях

━━━━━━━━━━━━━━━━━━━━━━━
⚠️ КОГДА СРОЧНО К ВРАЧУ:
━━━━━━━━━━━━━━━━━━━━━━━

🚨 Красные флаги:
   • Кровянистые выделения
   • Сильные боли в животе
   • Отёки лица и рук
   • Высокое давление
   • Малыш перестал шевелиться
   • Температура, озноб
   • Подтекание вод

━━━━━━━━━━━━━━━━━━━━━━━
💝 НОРМЫ НАБОРА ВЕСА:
━━━━━━━━━━━━━━━━━━━━━━━

📊 За весь 2-й триместр:
   • Худым девушкам: +5–6 кг
   • Нормальный вес: +4–5 кг
   • Полным девушкам: +3–4 кг

🌸 Главное: все назначения должен делать твой врач! Эта информация — для ознакомления.
"""
# Информация об анализах третьего триместра
THIRD_TRIMESTER_ANALYSES = """
📋 АНАЛИЗЫ ТРЕТЬЕГО ТРИМЕСТРА (28–41 неделя)

🌸 Финальный этап! Готовимся к встрече с малышом

━━━━━━━━━━━━━━━━━━━━━━━
🩺 28–30 НЕДЕЛЬ
━━━━━━━━━━━━━━━━━━━━━━━

🔹 Приём акушера-гинеколога — каждые 2 недели

👩‍⚕️ Дополнительные врачи:
   • Терапевт
   • Офтальмолог  
   • Стоматолог

🩸 Обследования:
   • Общий анализ крови
   • Общий анализ мочи

━━━━━━━━━━━━━━━━━━━━━━━
📊 30–34 НЕДЕЛИ
━━━━━━━━━━━━━━━━━━━━━━━

🔬 УЗИ 3-го триместра:
   • Оценка развития плода
   • Положение малыша (головное/тазовое)
   • Состояние плаценты
   • Количество околоплодных вод
   • Допплерометрия (кровоток)

━━━━━━━━━━━━━━━━━━━━━━━
💓 С 32 НЕДЕЛЬ
━━━━━━━━━━━━━━━━━━━━━━━

📈 КТГ (кардиотокография):
   • Оценка сердцебиения плода
   • Проводится раз в 2 недели или чаще
   • Проверяет, хватает ли малышу кислорода

━━━━━━━━━━━━━━━━━━━━━━━
🦠 35–37 НЕДЕЛЬ
━━━━━━━━━━━━━━━━━━━━━━━

🔬 Мазок на стрептококк группы B:
   • Рекомендован для профилактики инфекции новорожденного
   • Если положительный — в родах дадут антибиотик

━━━━━━━━━━━━━━━━━━━━━━━
💚 НОРМАЛЬНЫЕ СИМПТОМЫ В 3-М ТРИМЕСТРЕ
━━━━━━━━━━━━━━━━━━━━━━━

Эти состояния встречаются у большинства беременных и обычно не опасны, если они умеренные:

1. 🤰 Тренировочные схватки (Брэкстона-Хикса)
   • Нерегулярные
   • Не усиливаются
   • Проходят после отдыха или смены положения

2. 🌬 Одышка
   • Матка поднимает диафрагму
   • Чаще всего появляется после 30–32 недель
   • Проходит, когда живот опустится перед родами

3. 🔥 Изжога
   • Связана с расслаблением пищеводного сфинктера
   • Давление матки на желудок
   • Помогает дробное питание

4. 👣 Отёки ног к вечеру
   • Небольшие отёки стоп и лодыжек вечером — частое явление
   • Важно отличать от опасных отёков (лица, рук)

5. 🔙 Боли в тазу и пояснице
   • Связки растягиваются
   • Центр тяжести смещается

━━━━━━━━━━━━━━━━━━━━━━━
🚨 5 СИМПТОМОВ, ПРИ КОТОРЫХ НУЖНО СРОЧНО К ВРАЧУ
━━━━━━━━━━━━━━━━━━━━━━━

1. ⚡️ Сильная головная боль + мушки перед глазами
   • Отёки лица
   • Повышение давления
   • Тошнота
   → Может быть преэклампсия!

2. 👶 Резкое уменьшение движений ребёнка
   • Меньше 10 движений за 2 часа
   • Малыш не реагирует на еду или смену позы
   • Шевеления стали значительно слабее

3. 🩸 Кровянистые выделения
   • Любые, даже мажущие
   • Алый цвет

4. 💧 Подтекание или излитие вод
   • Прозрачная жидкость из влагалища
   • Ощущение влажности, не проходящее после туалета

5. ⏰ Регулярные болезненные схватки до 37 недель
   • Чаще 4-5 раз в час
   • Усиливаются со временем
   → Может быть преждевременными родами!

━━━━━━━━━━━━━━━━━━━━━━━
💡 ПРОСТОЙ ОРИЕНТИР:
━━━━━━━━━━━━━━━━━━━━━━━

Если появляется любой симптом, который резко отличается от обычного самочувствия, лучше лишний раз показаться врачу.

🌸 Береги себя и малыша! Скоро встреча! ❤️
"""

def _first_trimester_checklist_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    statuses = get_trimester_checklist_statuses(user_id, 1)
    text = f"{build_first_trimester_text(statuses)}\n\n{FIRST_TRIMESTER_DISCLAIMER}"
    keyboard = build_first_trimester_keyboard(statuses)
    keyboard.inline_keyboard.append(
        [
            InlineKeyboardButton(text="◀️ К триместрам", callback_data="menu_analyses"),
            InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu"),
        ]
    )
    return text, keyboard


async def send_first_trimester_checklist(
    message: Message,
    user_id: int,
    state: FSMContext,
    *,
    callback: CallbackQuery | None = None,
    keep_message: bool = False,
) -> None:
    text, keyboard = _first_trimester_checklist_view(user_id)
    if callback:
        await send_nav_screen_from_callback(
            callback,
            state,
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
            keep_message=keep_message,
        )
    else:
        await send_nav_screen(
            message,
            state,
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
            keep_message=keep_message,
        )


@dp.callback_query(lambda c: c.data == "first_trimester_analyses")
async def show_first_trimester_analyses(callback_query: types.CallbackQuery, state: FSMContext):
    """Показывает интерактивный чеклист анализов первого триместра."""
    persistent_origin = await is_persistent_message(state, callback_query.message.message_id)
    await send_first_trimester_checklist(
        callback_query.message,
        callback_query.from_user.id,
        state,
        callback=callback_query,
        keep_message=persistent_origin,
    )
    await callback_query.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("cl1p:"))
async def first_trimester_checklist_pick(callback: CallbackQuery):
    item_id = callback.data.split(":", 1)[1]
    item = FIRST_TRIMESTER_ITEM_BY_ID.get(item_id)
    if not item:
        await callback.answer("Пункт не найден", show_alert=True)
        return

    await callback.message.edit_reply_markup(
        reply_markup=build_first_trimester_status_keyboard(item_id),
    )
    await callback.answer(f"«{item['button']}» — выбери статус")


@dp.callback_query(lambda c: c.data and c.data.startswith("cl1s:"))
async def first_trimester_checklist_set(callback: CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return

    _, item_id, status = parts
    item = FIRST_TRIMESTER_ITEM_BY_ID.get(item_id)
    if not item or status not in STATUS_LABEL_RU:
        await callback.answer("Некорректный статус", show_alert=True)
        return

    user_id = callback.from_user.id
    set_trimester_checklist_status(user_id, 1, item_id, status)
    text, keyboard = _first_trimester_checklist_view(user_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer(f"«{item['button']}»: {STATUS_LABEL_RU[status]}")


@dp.callback_query(lambda c: c.data == "cl1back")
async def first_trimester_checklist_back(callback: CallbackQuery):
    user_id = callback.from_user.id
    text, keyboard = _first_trimester_checklist_view(user_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "second_trimester_analyses")
async def show_second_trimester_analyses(callback_query: types.CallbackQuery, state: FSMContext):
    persistent_origin = await is_persistent_message(state, callback_query.message.message_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К триместрам", callback_data="menu_analyses")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")],
        ]
    )
    await send_nav_screen_from_callback(
        callback_query,
        state,
        SECOND_TRIMESTER_ANALYSES,
        reply_markup=keyboard,
        parse_mode="Markdown",
        keep_message=persistent_origin,
    )
    await callback_query.answer()


@dp.callback_query(lambda c: c.data == "third_trimester_analyses")
async def show_third_trimester_analyses(callback_query: types.CallbackQuery, state: FSMContext):
    persistent_origin = await is_persistent_message(state, callback_query.message.message_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К триместрам", callback_data="menu_analyses")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")],
        ]
    )
    await send_nav_screen_from_callback(
        callback_query,
        state,
        THIRD_TRIMESTER_ANALYSES,
        reply_markup=keyboard,
        parse_mode="Markdown",
        keep_message=persistent_origin,
    )
    await callback_query.answer()


async def run_bot() -> None:
    init_db()
    if is_ai_configured():
        logger.info("DeepSeek AI: ключ задан, ответы ИИ включены")
    else:
        logger.warning("DeepSeek AI: DEEPSEEK_API_KEY не задан — ИИ-ответы отключены")
    if not EXPERT_CHAT_IDS:
        logger.warning("EXPERT_CHAT_IDS не задан — вопросы гинекологу не пересылаются")
    else:
        logger.info("EXPERT_CHAT_IDS: %d получатель(ей) настроено", len(EXPERT_CHAT_IDS))
    print("🚀 Бот запущен и ждет сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_bot())