"""Чеклист анализов и визитов по триместрам."""

from __future__ import annotations

import html
from typing import Dict, List, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

STATUS_NONE = "none"
STATUS_PLANNED = "planned"
STATUS_DONE = "done"

STATUS_EMOJI = {
    STATUS_NONE: "⚪",
    STATUS_PLANNED: "🟡",
    STATUS_DONE: "🟢",
}

STATUS_LABEL_RU = {
    STATUS_NONE: "Не сделано",
    STATUS_PLANNED: "Запланировано",
    STATUS_DONE: "Пройдено",
}

FIRST_TRIMESTER_DISCLAIMER = (
    "Важно: объём исследований может расширяться по показаниям🤍"
)

FIRST_TRIMESTER_ITEMS: List[dict] = [
    {
        "id": "obgyn",
        "button": "Акушер-гинеколог",
        "line": "Акушер-гинеколог (постановка на учёт до 12 недель)",
    },
    {"id": "therapist", "button": "Терапевт", "line": "Терапевт"},
    {"id": "dentist", "button": "Стоматолог", "line": "Стоматолог"},
    {"id": "ophthalmologist", "button": "Офтальмолог", "line": "Офтальмолог"},
    {"id": "ent", "button": "ЛОР", "line": "ЛОР"},
    {"id": "labs", "button": "Анализы", "line": "Анализы"},
    {
        "id": "screening",
        "button": "Скрининг 1 триместра",
        "line": "Скрининг 1 триместра 11–13.6 недель",
    },
]

FIRST_TRIMESTER_LAB_TESTS = [
    "Общий анализ крови",
    "Общий анализ мочи",
    "Группа крови + резус-фактор",
    "Антитела при Rh(-)",
    "ВИЧ",
    "Сифилис (RW)",
    "Гепатиты В и С",
    "Глюкоза крови",
    "Биохимия крови",
    "Коагулограмма",
    "ТТГ",
    "Мазок на флору / цитология",
]

FIRST_TRIMESTER_ITEM_BY_ID = {item["id"]: item for item in FIRST_TRIMESTER_ITEMS}


def _status_line(label: str, status: str) -> str:
    emoji = STATUS_EMOJI.get(status, STATUS_EMOJI[STATUS_NONE])
    safe = html.escape(label)
    if status == STATUS_DONE:
        return f"{emoji} <b>{safe}</b>"
    return f"{emoji} {safe}"


def build_first_trimester_text(statuses: Optional[Dict[str, str]] = None) -> str:
    statuses = statuses or {}

    doctor_lines = []
    for item in FIRST_TRIMESTER_ITEMS[:5]:
        status = statuses.get(item["id"], STATUS_NONE)
        doctor_lines.append(_status_line(item["line"], status))

    labs_status = statuses.get("labs", STATUS_NONE)
    labs_header = _status_line("Анализы:", labs_status)

    lab_lines = "\n".join(f"• {html.escape(name)}" for name in FIRST_TRIMESTER_LAB_TESTS)

    screening_status = statuses.get("screening", STATUS_NONE)
    screening_title = _status_line(
        "Скрининг 1 триместра 11–13.6 недель",
        screening_status,
    )

    return (
        "<b>1 ТРИМЕСТР</b> (до 13 недель и 6 дней)\n\n"
        "<b>Врачи:</b>\n"
        f"{chr(10).join(doctor_lines)}\n\n"
        f"{labs_header}\n"
        f"{lab_lines}\n\n"
        f"{screening_title}\n"
        "• УЗИ плода\n"
        "• Биохимический скрининг\n\n"
        "👇 Нажми на пункт ниже, чтобы отметить статус"
    )


def build_first_trimester_keyboard(
    statuses: Optional[Dict[str, str]] = None,
) -> InlineKeyboardMarkup:
    statuses = statuses or {}
    rows = []
    for item in FIRST_TRIMESTER_ITEMS:
        status = statuses.get(item["id"], STATUS_NONE)
        emoji = STATUS_EMOJI.get(status, STATUS_EMOJI[STATUS_NONE])
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {item['button']}",
                    callback_data=f"cl1p:{item['id']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_first_trimester_status_keyboard(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚪ Не сделано",
                    callback_data=f"cl1s:{item_id}:{STATUS_NONE}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟡 Запланировано",
                    callback_data=f"cl1s:{item_id}:{STATUS_PLANNED}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Пройдено",
                    callback_data=f"cl1s:{item_id}:{STATUS_DONE}",
                )
            ],
            [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="cl1back")],
        ]
    )
