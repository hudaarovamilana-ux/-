"""Расчёт срока беременности по якорным датам (дни от сегодня)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class PregnancyResult:
    week: int
    day: int
    total_days: int
    error: str | None = None
    warn_over_40: bool = False
    warn_over_42: bool = False


def _today() -> date:
    return datetime.now().date()


def _apply_warnings(week: int, day: int, total_days: int) -> PregnancyResult:
    warn42 = week > 42 or (week == 42 and day > 0)
    warn40 = week > 40 or (week == 40 and day > 0)
    return PregnancyResult(
        week=week,
        day=day,
        total_days=total_days,
        error=None,
        warn_over_40=warn40,
        warn_over_42=warn42,
    )


def from_days_since_anchor(days: int) -> PregnancyResult:
    if days < 0:
        return PregnancyResult(0, 0, days, error="Срок не может быть отрицательным: проверьте дату.")
    week = days // 7
    day = days % 7
    return _apply_warnings(week, day, days)


def from_lmp(lmp: date, today: date | None = None) -> PregnancyResult:
    t = today or _today()
    days = (t - lmp).days
    return from_days_since_anchor(days)


def from_conception(conception: date, today: date | None = None) -> PregnancyResult:
    return from_lmp(conception, today)


def from_due_date(edd: date, today: date | None = None) -> PregnancyResult:
    t = today or _today()
    days_to_birth = (edd - t).days
    passed_days = 280 - days_to_birth
    if passed_days < 0:
        return PregnancyResult(
            0,
            0,
            passed_days,
            error="По этой дате родов беременность ещё не наступила (проверьте дату).",
        )
    week = passed_days // 7
    day = passed_days % 7
    return _apply_warnings(week, day, passed_days)


def clamp_manual_week_day(week: int, day: int) -> tuple[int, int]:
    """Дни 0–6; недели для отображения в рамках 0–40 (сверх — обрежем только неделю для UI)."""
    d = max(0, min(6, day))
    w = max(0, min(40, week))
    return w, d


def approximate_due_from_total_days(total_days: int, today: date | None = None) -> str:
    """ПДР ≈ сегодня + (280 − уже прошедшие дни)."""
    t = today or _today()
    edd = t + timedelta(days=(280 - total_days))
    return edd.strftime("%Y-%m-%d")


def parse_dd_mm_yyyy(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def source_label_ru(source: str | None) -> str:
    return {
        "manual": "неделя и день (вручную)",
        "lmp": "дата последней менструации",
        "conception": "дата зачатия",
        "due_date": "предполагаемая дата родов",
    }.get(source or "", "не указано")
