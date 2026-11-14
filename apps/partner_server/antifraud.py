"""Антифрод-детекты (M5.2, threat-model.md §2, итерация I5).

Все детекты работают на фрагментах дня (Piece) и добавляют флаги —
начисление не блокируется, спор решает директор (кроме явных отказов
приема: replay/duplicate_seq на входе, опоздавшие события за грейсом).

Флаги согласованы с threat-model.md §2:
- night_autonomous_spike — автономная работа «за ночь» > night_spike_hours
  (деф. 6 ч); ночь = 22:00–24:00 ∪ 00:00–06:00 локального дня, threat-model §2 T-1;
- synthetic_input — равномерные подряд идущие интервалы (макросы/автокликеры);
- idle_no_signal — «супервизия» без сигналов ввода считается простоем;
- clock_skew — конец интервала позже приема больше max_clock_skew_sec;
- late_rejected — событие пришло позже grace_hours (T-09), не оплачивается.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from partner_cli import calc


@dataclass
class Piece:
    """Фрагмент дня до калькулятора: интервал в локальных секундах + метаданные."""

    interval: calc.Interval            # секунды от 00:00 локального дня
    cls: str
    start_dt: datetime                 # aware UTC
    end_dt: datetime                   # aware UTC
    signals: dict | None
    received_at: datetime | None       # aware UTC; None → нет heartbeat (импорт)


NIGHT_START_S = 22 * 3600             # 22:00 — начало ночного окна (вечер)
NIGHT_END_S = 6 * 3600                # 06:00 — конец ночного окна (утро)
SYNTHETIC_MIN_RUN = 4                  # ≥4 подряд одинаковых интервалов
SYNTHETIC_TOLERANCE_S = 5              # допуск «одинаковой» длительности


def _night_autonomous_seconds(pieces: list[Piece]) -> int:
    """Автономные секунды в ночном окне дня: [22:00, 24:00) ∪ [00:00, 06:00)."""
    total = 0
    for p in pieces:
        if p.cls != calc.CLASS_AUTONOMOUS:
            continue
        start, end = p.interval.start_s, p.interval.end_s
        # вечерний сегмент
        total += max(0, min(end, 86_400) - max(start, NIGHT_START_S))
        # утренний сегмент
        total += max(0, min(end, NIGHT_END_S) - max(start, 0))
    return total


def detect_night_spike(
    pieces: list[Piece], flags: list[str], night_spike_hours: float = 6.0
) -> None:
    """T-1: агент запущен на ночь. Флаг, начисление не меняется (капы решают деньги)."""
    if _night_autonomous_seconds(pieces) > night_spike_hours * 3600:
        flags.append("night_autonomous_spike")


def detect_synthetic_input(pieces: list[Piece], flags: list[str]) -> None:
    """T-2: синтетический ввод — серия одинаковых интервалов без вариативности."""
    run = 0
    prev_len = None
    prev_cls = None
    for p in pieces:
        if p.cls == calc.CLASS_IDLE:
            run, prev_len, prev_cls = 0, None, None
            continue
        length = p.interval.seconds
        if (
            prev_cls == p.cls
            and prev_len is not None
            and abs(length - prev_len) <= SYNTHETIC_TOLERANCE_S
        ):
            run += 1
        else:
            run = 1
        prev_len, prev_cls = length, p.cls
        if run >= SYNTHETIC_MIN_RUN:
            flags.append("synthetic_input")
            return


def detect_idle_no_signal(pieces: list[Piece], flags: list[str]) -> list[Piece]:
    """Idle heartbeat без событий → простой, не супервизия (M5.2)."""
    kept: list[Piece] = []
    changed = False
    for p in pieces:
        if (
            p.cls in (calc.CLASS_SUPERVISED, calc.CLASS_ACTIVE)
            and p.signals is not None
            and int(p.signals.get("input_events", 0) or 0) == 0
            and int(p.signals.get("focus", 0) or 0) == 0
        ):
            kept.append(
                Piece(
                    interval=calc.Interval(p.interval.start_s, p.interval.end_s, calc.CLASS_IDLE),
                    cls=calc.CLASS_IDLE,
                    start_dt=p.start_dt,
                    end_dt=p.end_dt,
                    signals=p.signals,
                    received_at=p.received_at,
                )
            )
            changed = True
        else:
            kept.append(p)
    if changed:
        flags.append("idle_no_signal")
    return kept


def detect_clock_skew_and_late(
    pieces: list[Piece],
    flags: list[str],
    grace_hours: float = 24.0,
    max_clock_skew_sec: float = 120.0,
) -> list[Piece]:
    """T-4 (clock_skew: серверное время авторитетно, флаг) и T-9 (late_rejected)."""
    kept: list[Piece] = []
    now = datetime.now(timezone.utc)
    for p in pieces:
        ref = p.received_at or now
        # конец интервала «в будущем» относительно приема больше допуска
        if (p.end_dt - ref).total_seconds() > max_clock_skew_sec:
            flags.append("clock_skew")
        # опоздавшие события за грейсом не оплачиваются (T-09)
        if (ref - p.end_dt).total_seconds() > grace_hours * 3600:
            flags.append("late_rejected")
            continue
        kept.append(p)
    return kept


def apply(
    pieces: list[Piece],
    *,
    night_spike_hours: float = 6.0,
    grace_hours: float = 24.0,
    max_clock_skew_sec: float = 120.0,
) -> tuple[list[calc.Interval], list[str]]:
    """Полный конвейер детектов. Возвращает интервалы для калькулятора + флаги."""
    flags: list[str] = []
    pieces = detect_clock_skew_and_late(
        pieces, flags, grace_hours=grace_hours, max_clock_skew_sec=max_clock_skew_sec
    )
    pieces = detect_idle_no_signal(pieces, flags)
    detect_night_spike(pieces, flags, night_spike_hours=night_spike_hours)
    detect_synthetic_input(pieces, flags)
    return [p.interval for p in pieces], sorted(set(flags))
