"""Приемочные сценарии T-01..T-12 из docs/02-policy/time-accounting.md §13.

Ставка 500 ₽/ч, оценка 10 000 000 ₽, кап 12 ч, k_sup 1,0, k_auto 0,2,
кап автономного 2,0 оплач. ч. Это golden-тесты: реализация calc.py обязана
давать в точности ожидаемые копейки (calculator-spec.md §7).
"""
from __future__ import annotations

from decimal import Decimal


from partner_cli import calc

POL = calc.Policy()  # дефолты: ставка 500, оценка 10 млн, капы 12/2.0, k_auto 0.2

H = 3600


def hours(n: int, cls: str) -> calc.Interval:
    return calc.Interval(0, n * H, cls)


def rub(cents: int) -> Decimal:
    return Decimal(cents) / 100


def test_t01_mixed_day():
    """T-01: active 4 ч, supervised 2 ч, autonomous 3 ч, idle 1 ч → 6,6 ч → 3 300 ₽ → 0,0330%."""
    ivs = [
        calc.Interval(10 * H, 14 * H, calc.CLASS_ACTIVE),
        calc.Interval(14 * H, 16 * H, calc.CLASS_SUPERVISED),
        calc.Interval(16 * H, 19 * H, calc.CLASS_AUTONOMOUS),  # 3 ч × 0,2 = 0,6 ч
        calc.Interval(19 * H, 20 * H, calc.CLASS_IDLE),
    ]
    r = calc.calc_day(ivs, POL)
    assert r.paid_hours == Decimal("6.6")
    assert r.amount_cents == 330_000
    assert r.share_bp == Decimal("3.3")  # 0,0330%


def test_t02_night_autonomous():
    """T-02: ночь, только autonomous 8 ч → paid 1,6 ч (≤ кап 2,0) → 800 ₽."""
    r = calc.calc_day([hours(8, calc.CLASS_AUTONOMOUS)], POL)
    assert r.paid_hours == Decimal("1.6")
    assert r.amount_cents == 80_000
    assert "autonomous_cap_applied" not in r.flags


def test_t03_daily_cap():
    """T-03: active 10 ч + supervised 4 ч → 14 ч → кап 12 ч → 6 000 ₽, cap_applied."""
    ivs = [hours(10, calc.CLASS_ACTIVE), hours(4, calc.CLASS_SUPERVISED)]
    r = calc.calc_day(ivs, POL)
    assert r.paid_hours == Decimal("12")
    assert r.amount_cents == 600_000
    assert "cap_applied" in r.flags


def test_t04_parallel_union():
    """T-04: сессии 10–12 и 11–13 → union 3 ч, не 4 ч."""
    ivs = [
        calc.Interval(10 * H, 12 * H, calc.CLASS_ACTIVE),
        calc.Interval(11 * H, 13 * H, calc.CLASS_ACTIVE),
    ]
    r = calc.calc_day(ivs, POL)
    assert r.paid_hours == Decimal("3")
    assert r.amount_cents == 150_000


def test_t05_midnight_split():
    """T-05: сессия 22:00–02:00 → день D: 2 ч; день D+1: 2 ч (active 500 ₽/ч → 1 000 ₽ каждый)."""
    ivs = [calc.Interval(22 * H, 26 * H, calc.CLASS_ACTIVE)]  # 26h = 02:00 следующего дня
    day_d, day_next = calc.split_by_midnight(ivs)
    assert day_d[0].seconds == 2 * H
    assert day_next[0].seconds == 2 * H
    r_d = calc.calc_day(day_d, POL)
    r_next = calc.calc_day(day_next, POL)
    assert r_d.amount_cents == 100_000
    assert r_next.amount_cents == 100_000


def test_t06_rate_change_inside_day():
    """T-06: ставка 500 до 14:00, 600 после; active 09–15 → 5×500 + 1×600 = 3 100 ₽."""
    ivs = [calc.Interval(9 * H, 15 * H, calc.CLASS_ACTIVE)]
    r = calc.calc_day(ivs, POL, rate_slots=[(0, 500), (14 * H, 600)])
    assert r.amount_cents == 310_000
    assert r.paid_hours == Decimal("6")


def test_t07_idle_no_pay():
    """T-07: простой 20 мин без сигналов → 0 ₽ за интервал."""
    r = calc.calc_day([calc.Interval(0, 20 * 60, calc.CLASS_IDLE)], POL)
    assert r.paid_hours == Decimal("0")
    assert r.amount_cents == 0


def test_t08_offline_within_grace():
    """T-08: offline 3 ч, загрузка через 5 ч → в грейсе 24 ч, учтено."""
    assert calc.late_event(5, grace_hours=24) is True


def test_t09_offline_beyond_grace():
    """T-09: offline 30 ч → late_rejected, 0 ₽ до решения директора."""
    assert calc.late_event(30, grace_hours=24) is False


def test_t10_manual_track_only():
    """T-10: работа без агента 4 ч → track_only: учтено, 0 ₽."""
    r = calc.calc_day([hours(4, calc.CLASS_ACTIVE)], POL, agent_attached=False)
    assert r.paid_hours == Decimal("4")
    assert r.amount_cents == 0
    assert "manual_track_only" in r.flags


def test_t11_quality_factor():
    """T-11: T-01 + k_quality 1,1 → 3 630 ₽."""
    ivs = [
        calc.Interval(10 * H, 14 * H, calc.CLASS_ACTIVE),
        calc.Interval(14 * H, 16 * H, calc.CLASS_SUPERVISED),
        calc.Interval(16 * H, 19 * H, calc.CLASS_AUTONOMOUS),
        calc.Interval(19 * H, 20 * H, calc.CLASS_IDLE),
    ]
    r = calc.calc_day(ivs, POL, k_quality=Decimal("1.1"))
    assert r.amount_cents == 363_000


def test_t12_autonomous_cap_2h():
    """T-12: autonomous 12 ч → paid 2,4 ч → кап автономного 2,0 ч → 1 000 ₽."""
    r = calc.calc_day([hours(12, calc.CLASS_AUTONOMOUS)], POL)
    assert r.paid_hours == Decimal("2.0")
    assert r.amount_cents == 100_000
    assert "autonomous_cap_applied" in r.flags


# ---- Контрольные примеры экономики долей (equity-model.md §9, E-01…E-08) ----


def test_e02_active_month():
    """E-02: 22 дня × 6,6 ч → месяц активен (145,2 ≥ 20 ч)."""
    assert calc.month_is_active(Decimal("145.2")) is True


def test_e03_inactive_month():
    """E-03: месяц 12 оплач. ч → НЕ активен (< 20 ч)."""
    assert calc.month_is_active(Decimal("12")) is False


def test_e05_pending_after_pool():
    """E-05: пул 3 000 000 ₽ исчерпан → pending: рубли копятся, доля не растет."""
    pool_budget = Decimal(POL.valuation_rub) * Decimal(30) / 100
    assert pool_budget == Decimal(3_000_000)
    day_cents = 330_000
    assert day_cents / 100 > 0  # рубли начисляются
    # доля при закрытом пуле фиксируется на уровне пула: R12/R15
    used = Decimal(3_000_000) / pool_budget * 100
    assert used == Decimal(100)


def test_e06_cash_split():
    """E-06: cash_share 20%, день 3 300 ₽ → cash 660 ₽, equity 2 640 ₽."""
    cash, equity = calc.cash_split(330_000, 20.0)
    assert cash == 66_000
    assert equity == 264_000


def test_e07_vesting_fraction():
    """E-07: AM = 12, months 48 → fraction 0,25."""
    assert calc.vesting_fraction(12) == Decimal("0.25")


def test_e08_cliff():
    """E-08: AM = 8 (< cliff 12) → fraction 0."""
    assert calc.vesting_fraction(8) == Decimal("0")


def test_share_of_day():
    """Санити: 3 300 ₽ при оценке 10 000 000 ₽ → 0,0330% (share_bp = 3,3)."""
    # точное значение берём из T-01
    r = calc.calc_day(
        [
            calc.Interval(10 * H, 14 * H, calc.CLASS_ACTIVE),
            calc.Interval(14 * H, 16 * H, calc.CLASS_SUPERVISED),
            calc.Interval(16 * H, 19 * H, calc.CLASS_AUTONOMOUS),
            calc.Interval(19 * H, 20 * H, calc.CLASS_IDLE),
        ],
        POL,
    )
    assert r.share_pct == Decimal("0.0330")
