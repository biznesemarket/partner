"""Калькулятор дня «Партнер» — нормативная реализация правил R1–R16.

Реализует docs/02-policy/time-accounting.md §4 и calculator-spec.md.
Все вычисления в целых секундах и копейках; дробные коэффициенты — Decimal.
Приемочные сценарии T-01..T-12 закреплены в tests/test_calc.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

H = Decimal(3600)

CLASS_ACTIVE = "active_human"
CLASS_SUPERVISED = "supervised_agent"
CLASS_AUTONOMOUS = "autonomous_agent"
CLASS_IDLE = "idle"

# Классы, дающие оплату, в порядке приоритета усечения (R5: active → supervised → autonomous)
PAID_CLASSES = (CLASS_ACTIVE, CLASS_SUPERVISED, CLASS_AUTONOMOUS)
# Порядок усечения при дневном капе: сначала жертвуем автономным
TRUNCATION_ORDER = (CLASS_AUTONOMOUS, CLASS_SUPERVISED, CLASS_ACTIVE)


@dataclass(frozen=True)
class Interval:
    """Интервал в секундах от начала суток (пояса проекта)."""

    start_s: int
    end_s: int
    cls: str

    @property
    def seconds(self) -> int:
        return max(0, self.end_s - self.start_s)


@dataclass(frozen=True)
class Policy:
    """Подмножество policy.yaml, нужное калькулятору."""

    rate_rub_per_hour: int = 500
    valuation_rub: int = 10_000_000
    daily_cap_hours: float = 12.0
    autonomous_paid_cap_hours: float = 2.0
    k_supervised: float = 1.0
    k_autonomous: float = 0.2
    manual_only_policy: str = "track_only"
    cash_share_pct: float = 0.0


@dataclass
class DayResult:
    paid_hours: Decimal
    amount_cents: int
    share_bp: Decimal          # доля в базисных пунктах (1 bp = 0,01 п.п.)
    flags: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)

    @property
    def amount_rub(self) -> Decimal:
        return Decimal(self.amount_cents) / 100

    @property
    def share_pct(self) -> Decimal:
        return self.share_bp / 100


def union_intervals(intervals: list[Interval]) -> list[Interval]:
    """R3: пересекающиеся интервалы одного класса объединяются, не суммируются."""
    merged: list[Interval] = []
    for iv in sorted(intervals, key=lambda x: (x.cls, x.start_s, x.end_s)):
        if merged and merged[-1].cls == iv.cls and iv.start_s <= merged[-1].end_s:
            prev = merged[-1]
            merged[-1] = Interval(prev.start_s, max(prev.end_s, iv.end_s), prev.cls)
        else:
            merged.append(Interval(iv.start_s, iv.end_s, iv.cls))
    return merged


def split_by_rate(
    intervals: list[Interval], rate_slots: list[tuple[int, int]]
) -> list[tuple[Interval, int]]:
    """R14: разрезать интервалы по границам смены ставки.

    rate_slots — отсортированные пары (секунда_начала_действия, ставка).
    """
    slots = sorted(rate_slots, key=lambda x: x[0])
    pieces: list[tuple[Interval, int]] = []
    for iv in intervals:
        for i, (slot_start, rate) in enumerate(slots):
            slot_end = slots[i + 1][0] if i + 1 < len(slots) else 86_400
            lo, hi = max(iv.start_s, slot_start), min(iv.end_s, slot_end)
            if lo < hi:
                pieces.append((Interval(lo, hi, iv.cls), rate))
    return pieces


def _raw_paid_seconds(intervals: list[Interval], pol: Policy) -> dict[str, Decimal]:
    """Оплачиваемые секунды по классам с применением коэффициентов (шаг 4 §4)."""
    by_cls: dict[str, Decimal] = {c: Decimal(0) for c in PAID_CLASSES}
    for iv in intervals:
        if iv.cls in by_cls:
            by_cls[iv.cls] += Decimal(iv.seconds)
    raw = {
        CLASS_ACTIVE: by_cls[CLASS_ACTIVE],
        CLASS_SUPERVISED: by_cls[CLASS_SUPERVISED] * Decimal(str(pol.k_supervised)),
        CLASS_AUTONOMOUS: by_cls[CLASS_AUTONOMOUS] * Decimal(str(pol.k_autonomous)),
    }
    return raw


def _apply_caps(
    paid: dict[str, Decimal], pol: Policy, flags: list[str]
) -> dict[str, Decimal]:
    """R5: кап автономного класса, затем дневной кап с приоритетом усечения."""
    paid = dict(paid)
    auto_cap = Decimal(str(pol.autonomous_paid_cap_hours)) * H
    if paid[CLASS_AUTONOMOUS] > auto_cap:
        paid[CLASS_AUTONOMOUS] = auto_cap
        flags.append("autonomous_cap_applied")
    total_cap = Decimal(str(pol.daily_cap_hours)) * H
    total = sum(paid.values())
    if total > total_cap:
        excess = total - total_cap
        for cls in TRUNCATION_ORDER:
            if excess <= 0:
                break
            take = min(paid[cls], excess)
            paid[cls] -= take
            excess -= take
        flags.append("cap_applied")
    return paid


class CalcError(Exception):
    """Нормативно-недопустимая конфигурация расчета (T-05)."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


def calc_day(
    intervals: list[Interval],
    pol: Policy = Policy(),
    rate_slots: list[tuple[int, int]] | None = None,
    k_quality: Decimal | float = 1.0,
    agent_attached: bool = True,
) -> DayResult:
    """Полный расчет дня: union → разрез по ставке → капы → сумма (R1)."""
    flags: list[str] = []
    if not agent_attached and pol.manual_only_policy == "track_only":
        # R-отчет: работа без агента учитывается, но не оплачивается (T-10)
        tracked = sum(iv.seconds for iv in intervals if iv.cls in PAID_CLASSES)
        return DayResult(
            paid_hours=Decimal(tracked) / H,
            amount_cents=0,
            share_bp=Decimal(0),
            flags=["manual_track_only"],
            breakdown={"note": "track_only: учтено, не оплачивается"},
        )
    if not agent_attached and pol.manual_only_policy == "ignore":
        return DayResult(Decimal(0), 0, Decimal(0), flags=["manual_ignored"], breakdown={})
    if not agent_attached and pol.manual_only_policy == "bill":
        raise CalcError(
            "manual_only_policy_not_supported",
            "Режим manual_only_policy=bill не реализован: требуется ADR; "
            "используйте track_only или ignore",
        )

    merged = union_intervals(intervals)
    pieces = split_by_rate(merged, rate_slots or [(0, pol.rate_rub_per_hour)])

    # Сырые оплачиваемые секунды по классам и слотам ставки
    raw_by_slot: list[dict[str, Decimal]] = []
    for iv, _rate in pieces:
        raw_by_slot.append(_raw_paid_seconds([iv], pol))

    # Капы применяются к дневным суммам каждого класса
    raw_totals = {
        cls: sum(slot[cls] for slot in raw_by_slot) for cls in PAID_CLASSES
    }
    capped = _apply_caps(raw_totals, pol, flags)

    # Распределение усеченных секунд по слотам ставки пропорционально сырым секундам
    kq = Decimal(str(k_quality))
    amount_cents = Decimal(0)
    paid_hours_total = Decimal(0)
    for i, (_iv, rate) in enumerate(pieces):
        piece_seconds = Decimal(0)
        for cls in PAID_CLASSES:
            raw_cls_slot = raw_by_slot[i][cls]
            raw_cls_total = raw_totals[cls]
            if raw_cls_total > 0:
                share = raw_cls_slot / raw_cls_total
                piece_seconds += capped[cls] * share
        piece_hours = piece_seconds / H
        paid_hours_total += piece_hours
        amount_cents += (piece_hours * Decimal(rate) * 100 * kq).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )

    # R7: доля в базисных пунктах. share_pct = (amount_cents/100)/valuation*100
    # = amount_cents/valuation; share_bp = share_pct × 100
    share_bp = (Decimal(amount_cents) / Decimal(pol.valuation_rub) * Decimal(100)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    breakdown = {cls: str(capped[cls] / H) for cls in PAID_CLASSES}
    return DayResult(paid_hours_total, int(amount_cents), share_bp, flags, breakdown)


def split_by_midnight(intervals: list[Interval]) -> tuple[list[Interval], list[Interval]]:
    """Шаг 3 §4: разрез интервалов по границе суток (T-05).

    Интервалы задаются в секундах от 00:00 дня D; значения > 86400 относятся к дню D+1.
    """
    day_d, day_next = [], []
    for iv in intervals:
        if iv.end_s <= 86_400:
            day_d.append(iv)
        elif iv.start_s >= 86_400:
            day_next.append(Interval(iv.start_s - 86_400, iv.end_s - 86_400, iv.cls))
        else:
            day_d.append(Interval(iv.start_s, 86_400, iv.cls))
            day_next.append(Interval(0, iv.end_s - 86_400, iv.cls))
    return day_d, day_next


def late_event(delta_hours: float, grace_hours: float = 24.0) -> bool:
    """§7: прием опоздавших событий. True — в грейсе (учтено), False — late_rejected."""
    return delta_hours <= grace_hours


def vesting_fraction(active_months: int, months: int = 48, cliff_months: int = 12) -> Decimal:
    """ADR-0019: fraction вестинга по активным месяцам."""
    if active_months < cliff_months:
        return Decimal(0)
    return Decimal(min(active_months, months)) / Decimal(months)


def month_is_active(paid_hours_month: Decimal, min_paid_hours: float = 20.0) -> bool:
    """ADR-0019: активный месяц в оплачиваемых часах."""
    return paid_hours_month >= Decimal(str(min_paid_hours))


def cash_split(amount_cents: int, cash_share_pct: float) -> tuple[int, int]:
    """R8: денежная и долевая части дня. Возвращает (cash_cents, equity_cents)."""
    cash = (Decimal(amount_cents) * Decimal(str(cash_share_pct)) / 100).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return int(cash), int(amount_cents - cash)
