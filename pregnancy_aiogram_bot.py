from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram import BaseMiddleware
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from typing import Any, Awaitable, Callable, Dict
from datetime import datetime, timedelta
import asyncio
import os

# Импортируем данные по неделям
from weeks_data import WEEKS_INFO

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
    count_users,
    ensure_user_exists,
    get_kick_history,
    get_today_kicks,
    get_user,
    init_db,
    log_message,
    refresh_computed_pregnancy,
    start_kick_count,
    update_notifications,
    update_profile_field,
    user_has_complete_onboarding,
)
from pregnancy_math import (
    approximate_due_from_total_days,
    clamp_manual_week_day,
    from_conception,
    from_due_date,
    from_lmp,
    parse_dd_mm_yyyy,
    source_label_ru,
)
from scheduler import check_week_updates

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.startup()
async def _start_week_scheduler(bot: Bot, **_kwargs: Any) -> None:
    asyncio.create_task(check_week_updates(bot))


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


def get_onboarding_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Неделя беременности", callback_data="onb_pick_manual")],
            [InlineKeyboardButton(text="Дата последней менструации", callback_data="onb_pick_lmp")],
            [InlineKeyboardButton(text="Дата зачатия", callback_data="onb_pick_conception")],
            [InlineKeyboardButton(text="Предполагаемая дата родов", callback_data="onb_pick_due")],
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="onb_cancel")],
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
    await message.answer("Что из этого вы знаете?", reply_markup=types.ReplyKeyboardRemove())
    await message.answer("Выбери вариант:", reply_markup=get_onboarding_method_keyboard())


@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    if user_has_complete_onboarding(user):
        await message.answer(
            "🌸 Привет! Выбери раздел в меню ниже.",
            reply_markup=get_main_menu_keyboard(),
        )
        return
    await show_onboarding_pick(message, state)


@dp.message(Command("stats"))
async def stats(message: types.Message):
    total = count_users()
    await message.answer(f"📊 Всего пользователей: {total}")


@dp.callback_query(lambda c: c.data == "onb_cancel")
async def onb_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("📌 Главное меню:", reply_markup=get_main_menu_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "onb_back_pick")
async def onb_back_pick(callback: types.CallbackQuery, state: FSMContext):
    await show_onboarding_pick(callback.message, state)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "onb_pick_manual")
async def onb_pick_manual(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.onb_manual_week)
    await callback.message.answer(
        "Выбери неделю беременности (1–41):",
        reply_markup=get_manual_week_onboarding_keyboard(),
    )
    await callback.answer()


@dp.callback_query(StateFilter(PregnancyStates.onb_manual_week), lambda c: c.data.startswith("onbm_"))
async def onb_manual_week_chosen(callback: types.CallbackQuery, state: FSMContext):
    week = int(callback.data.split("_")[1])
    await state.update_data(onb_manual_week=week)
    await state.set_state(PregnancyStates.onb_manual_day)
    await callback.message.answer(
        f"Неделя: {week}. Теперь выбери день внутри недели (0–6):",
        reply_markup=get_manual_day_onboarding_keyboard(),
    )
    await callback.answer()


@dp.callback_query(StateFilter(PregnancyStates.onb_manual_day), lambda c: c.data == "onb_back_manual_week")
async def onb_back_manual_week(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.onb_manual_week)
    await callback.message.answer(
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
        extra = "\n⚠️ Срок больше 40 недель — уточни у врача.\n"
    if week_raw > 42:
        extra += "\n⚠️ Срок больше 42 недель — проверь введённые данные.\n"
    await callback.message.answer(
        f"Ваша беременность:\n**{w}** неделя **{d}** день{extra}",
        parse_mode="Markdown",
    )
    info_week = max(1, min(w, 41))
    await show_week_info(callback.message, info_week)
    await callback.message.answer("📌 Главное меню:", reply_markup=get_main_menu_keyboard())
    await callback.answer()


async def _prompt_date_input(callback: types.CallbackQuery, state: FSMContext, kind: str, prompt: str) -> None:
    await state.set_state(PregnancyStates.onb_enter_date)
    await state.update_data(onb_anchor=kind)
    await callback.message.answer(prompt, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "onb_pick_lmp")
async def onb_pick_lmp(callback: types.CallbackQuery, state: FSMContext):
    await _prompt_date_input(
        callback,
        state,
        "lmp",
        "Введите дату последней менструации в формате **ДД.ММ.ГГГГ** (например, 15.06.2025):",
    )


@dp.callback_query(lambda c: c.data == "onb_pick_conception")
async def onb_pick_conception(callback: types.CallbackQuery, state: FSMContext):
    await _prompt_date_input(
        callback,
        state,
        "conception",
        "Введите дату зачатия в формате **ДД.ММ.ГГГГ**:",
    )


@dp.callback_query(lambda c: c.data == "onb_pick_due")
async def onb_pick_due(callback: types.CallbackQuery, state: FSMContext):
    await _prompt_date_input(
        callback,
        state,
        "due_date",
        "Введите предполагаемую дату родов в формате **ДД.ММ.ГГГГ**:",
    )


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
    await message.answer(
        f"Ваша беременность:\n**{res.week}** неделя **{res.day}** день"
        + (f"\n{warn}" if warn else ""),
        parse_mode="Markdown",
    )
    info_week = max(1, min(res.week, 41))
    await show_week_info(message, info_week)
    await message.answer("📌 Главное меню:", reply_markup=get_main_menu_keyboard())


@dp.message(PregnancyStates.onb_pick_method)
async def onb_pick_method_fallback(message: types.Message):
    await message.answer("Пожалуйста, выбери вариант кнопками под предыдущим сообщением.")


@dp.message(PregnancyStates.onb_manual_week)
async def onb_manual_week_text_fallback(message: types.Message):
    await message.answer("Выбери неделю кнопками с числами (1–41).")


@dp.message(PregnancyStates.onb_manual_day)
async def onb_manual_day_text_fallback(message: types.Message):
    await message.answer("Выбери день кнопками 0–6.")
@dp.message(lambda message: message.text == "📅 Недели")
async def show_weeks_menu(message: types.Message):
    """Показывает меню выбора недели"""
    await message.answer(
        "🌸 Выбери неделю беременности:",
        reply_markup=get_all_weeks_keyboard()
    )

# Кнопка «Анализы»
@dp.message(lambda message: message.text == "📋 Анализы")
async def handle_analyses_button(message: types.Message):
    await message.answer(
        "📋 Выбери триместр:",
        reply_markup=get_analyses_menu_keyboard()
    )

# Кнопка «Старт»
@dp.message(lambda message: message.text == "🏠 Старт")
async def handle_start_button(message: types.Message, state: FSMContext):
    await state.clear()
    await start(message, state)


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
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="profile_back_main")])
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


async def send_profile_message(message: types.Message) -> None:
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
        preg_line = f"{week_num} неделя {day_num} день"

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
    await message.answer(profile_text, parse_mode="Markdown", reply_markup=keyboard)


@dp.message(lambda message: message.text == "👤 Профиль")
async def handle_profile_button(message: types.Message, state: FSMContext):
    await state.clear()
    await send_profile_message(message)


@dp.callback_query(lambda c: c.data == "profile_show")
async def profile_show_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await send_profile_message(callback.message)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_back_main")
async def profile_back_main_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("📌 Главное меню:", reply_markup=get_main_menu_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_add_info")
async def profile_add_info_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "📝 Что хотите добавить или изменить?",
        reply_markup=get_profile_add_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_week")
async def profile_edit_week_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_week)
    await callback.message.answer("Введите неделю беременности (1-42):")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_height")
async def profile_edit_height_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_height)
    await callback.message.answer("Введите ваш рост (в см):")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_weight")
async def profile_edit_weight_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_weight)
    await callback.message.answer("Введите ваш вес (в кг):")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile_edit_name")
async def profile_edit_name_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PregnancyStates.waiting_profile_name)
    await callback.message.answer("Введите имя:")
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
    await message.answer("Неделя сохранена ✅")
    await send_profile_message(message)


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
    await message.answer("Рост сохранён ✅")
    await send_profile_message(message)


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
    await message.answer("Вес сохранён ✅")
    await send_profile_message(message)


@dp.message(PregnancyStates.waiting_profile_name)
async def save_profile_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым.")
        return
    update_profile_field(message.from_user.id, "username", name[:64])
    await state.clear()
    await message.answer("Имя сохранено ✅")
    await send_profile_message(message)


@dp.callback_query(lambda c: c.data.startswith("week_"))
async def show_week_info_from_menu(callback: types.CallbackQuery):
    """Показывает информацию о выбранной неделе"""
    week = int(callback.data.split("_")[1])
    week_data = get_week_info(week)
    
    # Формируем текст
    text = f"🌸 **{week} неделя беременности**\n\n"
    
    if week_data.get('fruit'):
        text += f"🍎 Размер плода: {week_data['fruit']}\n\n"
    
    if week_data.get('description'):
        text += f"{week_data['description']}\n\n"
    
    if week_data.get('mom_feeling'):
        text += f"🤰 **Ощущения мамы:**\n{week_data['mom_feeling']}\n\n"
    
    if week_data.get('nutrition'):
        text += f"🥗 **Питание:**\n{week_data['nutrition']}\n\n"
    
    if week_data.get('doctors'):
        text += f"👩‍⚕️ **Врачи и анализы:**\n{week_data['doctors']}\n\n"
    
    # Отправляем основную информацию
    await callback.message.answer(text, parse_mode="Markdown")
    
    # Если есть интересный факт
    if week_data.get('fact'):
        await callback.message.answer(
            f"✨ **Интересный факт:**\n{week_data['fact']}",
            parse_mode="Markdown"
        )
    
    # Предлагаем анализы по триместру
    if week <= 12:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Анализы 1-го триместра", callback_data="first_trimester_analyses")]
        ])
        await callback.message.answer("📋 Хочешь узнать об анализах первого триместра?", reply_markup=keyboard)
    elif 13 <= week <= 27:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Анализы 2-го триместра", callback_data="second_trimester_analyses")]
        ])
        await callback.message.answer("📋 Хочешь узнать об анализах второго триместра?", reply_markup=keyboard)
    elif 28 <= week <= 41:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Анализы 3-го триместра", callback_data="third_trimester_analyses")]
        ])
        await callback.message.answer("📋 Хочешь узнать об анализах третьего триместра?", reply_markup=keyboard)
    
    await callback.answer()
@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_main_menu(callback: types.CallbackQuery):
    """Возвращает в главное меню"""
    await callback.message.answer(
        "📌 Главное меню:",
        reply_markup=get_main_menu_keyboard()
    )
    await callback.answer()
@dp.callback_query(lambda c: c.data.startswith("analyses_"))
async def show_analyses_by_trimester(callback: types.CallbackQuery):
    trimester = int(callback.data.split("_")[1])

    if trimester == 1:
        text = FIRST_TRIMESTER_ANALYSES
    elif trimester == 2:
        text = SECOND_TRIMESTER_ANALYSES
    else:
        text = THIRD_TRIMESTER_ANALYSES

    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()
@dp.callback_query(lambda c: c.data in ["notif_on", "notif_off"])
async def set_notifications(callback: types.CallbackQuery):
    from database import update_notifications
    
    enabled = 1 if callback.data == "notif_on" else 0
    update_notifications(callback.from_user.id, enabled)
    
    status = "включены" if enabled else "выключены"
    await callback.message.answer(f"✅ Уведомления {status}")
    await callback.answer()
@dp.message(lambda message: message.text == "🔔 Уведомления")
async def notifications_settings(message: types.Message):
    """Настройка уведомлений"""
    try:
        from database import get_user
        
        user = get_user(message.from_user.id)
        
        # ЕСЛИ ПОЛЬЗОВАТЕЛЯ НЕТ - ГОВОРИМ ВВЕСТИ НЕДЕЛЮ
        if not user:
            await message.answer("❌ Сначала введи свою неделю беременности через /start!")
            return
        
        # Получаем статус уведомлений (индекс 8 = notifications_enabled)
        notifications_enabled = user[U_NOTIF]
        status = "✅ Включены" if notifications_enabled == 1 else "❌ Выключены"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Включить", callback_data="notif_on")],
            [InlineKeyboardButton(text="❌ Выключить", callback_data="notif_off")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
        ])
        
        await message.answer(
            f"🔔 **Настройки уведомлений**\n\n"
            f"Статус: {status}\n"
            f"Твоя неделя: {user[U_WEEK]}\n\n"
            f"Я буду напоминать тебе о новой неделе каждые 7 дней!",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        print(f"❌ Ошибка в notifications_settings: {e}")
@dp.message(lambda message: message.text == "👶 Подсчет шевелений")
async def kick_counter_menu(message: types.Message):
    """Меню подсчета шевелений"""
    refresh_computed_pregnancy(message.from_user.id)
    user = get_user(message.from_user.id)

    if not user or user[U_WEEK] is None:
        await message.answer("❌ Сначала укажи срок через /start!")
        return

    current_week = user[U_WEEK]
    print(f"📊 Текущая неделя пользователя: {current_week}")
    
    # 👇 ПРОВЕРЯЕМ НЕДЕЛЮ И ПОКАЗЫВАЕМ РАЗНЫЕ СООБЩЕНИЯ
    if current_week < 28:
        # Если неделя меньше 28 - показываем информационное сообщение
        await message.answer(
            f"🌸 У тебя сейчас {current_week} неделя.\n\n"
            f"Обычно шевеления становятся регулярными и хорошо ощущаются с 28 недели.\n"
            f"Но ты уже можешь практиковаться! 👶✨"
        )
    
    # 👇 А ЭТО ПОКАЗЫВАЕМ ВСЕМ (И С 28 НЕДЕЛИ, И РАНЬШЕ)
    # Начинаем подсчет за сегодня
    start_kick_count(message.from_user.id)
    today_kicks = get_today_kicks(message.from_user.id)
    
    text = (
        f"👶 **Подсчет шевелений**\n\n"
        f"Нажимай кнопку каждый раз,\n"
        f"когда чувствуешь движение малыша 🤍\n\n"
        f"📊 Сегодня: {today_kicks} раз"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Малыш пошевелился", callback_data="add_kick")],
        [InlineKeyboardButton(text="📈 Итог за 2 часа", callback_data="check_2h")],
        [InlineKeyboardButton(text="📅 История", callback_data="kick_history")],
        [InlineKeyboardButton(text="ℹ️ О норме", callback_data="kick_info")]
    ])
    
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

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
        reply_markup=callback.message.reply_markup,
        parse_mode="Markdown"
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
    
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "kick_history")
async def show_kick_history(callback: types.CallbackQuery):
    """Показывает историю шевелений"""
    from database import get_kick_history
    
    history = get_kick_history(callback.from_user.id, days=7)
    
    if not history:
        await callback.message.answer("📅 Пока нет данных. Начни подсчет сегодня!")
        await callback.answer()
        return
    
    text = "📅 **История шевелений за 7 дней**\n\n"
    for date, count in history:
        # Форматируем дату
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d.%m")
        text += f"• {formatted_date}: {count} раз\n"
    
    await callback.message.answer(text, parse_mode="Markdown")
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
    
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()
def get_main_menu_keyboard():
    """Создает клавиатуру главного меню"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Недели")],
            [KeyboardButton(text="📋 Анализы")],
            [KeyboardButton(text="👶 Подсчет шевелений")],  # Новая кнопка
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🔔 Уведомления")],
            [KeyboardButton(text="🏠 Старт")]
        ],
        resize_keyboard=True
    )
    return keyboard
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
        ]
    )
    return keyboard

async def show_week_info(message: types.Message, week: int):
    # Получаем информацию о неделе
    week_data = get_week_info(week)
    
    # Формируем полное сообщение
    response = f"🌸 **{week} неделя беременности**\n\n"
    
    if week_data.get('fruit'):
        response += f"🍎 Размер плода: {week_data['fruit']}\n\n"
    
    if week_data.get('description'):
        response += f"{week_data['description']}\n\n"
    
    if week_data.get('mom_feeling'):
        response += f"🤰 **Ощущения мамы:**\n{week_data['mom_feeling']}\n\n"
    
    if week_data.get('nutrition'):
        response += f"🥗 **Питание:**\n{week_data['nutrition']}\n\n"
    
    if week_data.get('doctors'):
        response += f"👩‍⚕️ **Врачи и анализы:**\n{week_data['doctors']}\n\n"
    
    await message.answer(response, parse_mode="Markdown")
    
    # Если есть интересный факт - показываем его отдельно
    if week_data and week_data.get('fact'):
        await message.answer(f"✨ **Интересный факт:**\n{week_data['fact']}", parse_mode="Markdown")
    
    # Показываем кнопку с анализами по триместрам
    if week <= 12:
        keyboard = get_first_trimester_analyses_keyboard()
        await message.answer("📋 **Хочешь узнать об анализах первого триместра?**", reply_markup=keyboard)
    elif 13 <= week <= 27:
        keyboard = get_second_trimester_analyses_keyboard()
        await message.answer("📋 **Хочешь узнать об анализах второго триместра?**", reply_markup=keyboard)
    elif 28 <= week <= 41:
        keyboard = get_third_trimester_analyses_keyboard()
        await message.answer("📋 **Хочешь узнать об анализах третьего триместра?**", reply_markup=keyboard)
    
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

def get_first_trimester_analyses_keyboard():
    """Создает кнопку для просмотра анализов первого триместра"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Анализы 1-го триместра", callback_data="first_trimester_analyses")]
        ]
    )
    return keyboard
def get_second_trimester_analyses_keyboard():
    """Создает кнопку для просмотра анализов второго триместра"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Анализы 2-го триместра", callback_data="second_trimester_analyses")]
        ]
    )
    return keyboard
def get_third_trimester_analyses_keyboard():
    """Создает кнопку для просмотра анализов третьего триместра"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Анализы 3-го триместра", callback_data="third_trimester_analyses")]
        ]
    )
    return keyboard
def calculate_current_week(registered_date, initial_week):
    """Рассчитывает текущую неделю на основе даты регистрации"""
    if isinstance(registered_date, str):
        registered_date = datetime.strptime(registered_date, "%Y-%m-%d %H:%M:%S")
    
    days_passed = (datetime.now() - registered_date).days
    weeks_passed = days_passed // 7
    current_week = initial_week + weeks_passed
    
    return min(current_week, 42)  # Не больше 42 недель

# Информация об анализах первого триместра
FIRST_TRIMESTER_ANALYSES = """
📋 Анализы первого триместра

1️⃣ Тест на беременность (домашний)
   • Первый признак - задержка менструации
   • Можно делать с первого дня задержки

2️⃣ Первый анализ ХГЧ (кровь из вены)
   • Подтверждает беременность
   • Показывает примерный срок

3️⃣ Повторный ХГЧ через 48 часов
   • При нормальной беременности уровень ХГЧ удваивается каждые 48-72 часа
   • Помогает исключить замершую беременность

4️⃣ УЗИ на 5–6 неделе
   • Подтверждает маточную беременность
   • Можно увидеть плодное яйцо и сердцебиение
   • Исключает внематочную беременность

5️⃣ Первый скрининг (12 недель)
   • УЗИ + анализ крови
   • Проверка на хромосомные abnormalities
   • Оценка развития малыша 

🌸 Важно: все назначения должен делать твой врач! Эта информация - для ознакомления.
"""
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

@dp.callback_query(lambda c: c.data == "first_trimester_analyses")
async def show_first_trimester_analyses(callback_query: types.CallbackQuery):
    """Показывает информацию об анализах первого триместра"""
    await callback_query.message.answer(
        FIRST_TRIMESTER_ANALYSES,
        parse_mode="Markdown"
    )
    await callback_query.answer()  # Закрываем уведомление о нажатии
@dp.callback_query(lambda c: c.data == "second_trimester_analyses")
async def show_second_trimester_analyses(callback_query: types.CallbackQuery):
    """Показывает информацию об анализах второго триместра"""
    await callback_query.message.answer(
        SECOND_TRIMESTER_ANALYSES,
        parse_mode="Markdown"
    )
    await callback_query.answer()

@dp.callback_query(lambda c: c.data == "third_trimester_analyses")
async def show_third_trimester_analyses(callback_query: types.CallbackQuery):
    """Показывает информацию об анализах третьего триместра"""
    await callback_query.message.answer(
        THIRD_TRIMESTER_ANALYSES,
        parse_mode="Markdown"
    )
    await callback_query.answer()


async def run_bot() -> None:
    init_db()
    print("🚀 Бот запущен и ждет сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_bot())