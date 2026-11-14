"""Дайджесты (M6.1 дневной, M6.2 месячный): числа только из БД.

DoD итерации: «дайджест дня содержит только числа, воспроизводимые из БД».
- суммы — из day_records (закрытый день, append-only) и adjustments;
- часы по классам — пересборка из интервалов БД через partner_cli.calc
  (golden-калькулятор T-01..T-12) — детерминированно по записям БД;
- флаги — из таблицы flags; спорные интервалы — из открытых споров дня.
Текст дайджеста рендерится детерминированно (render_*_digest) — тот же
текст попадает в уведомление (notify.py) и в CLI.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from partner_cli import calc

from . import models, service

DEFAULT_POOL_PCT = 30.0  # динамический пул долей (equity-model.md)


class DigestError(service.DomainError):
    """Ошибка дайджеста — тот же контракт, что у сервисных ошибок."""


# ---------- дневной дайджест (M6.1) ----------


def day_digest(sess: Session, project: models.Project, participant_id: str, date_iso: str) -> dict:
    """Дайджест закрытого дня: часы по классам, ₽, доля %, флаги, споры."""
    day_id = f"{participant_id}|{date_iso}"
    rec = sess.get(models.DayRecord, day_id)
    if rec is None or not rec.closed:
        raise DigestError(
            "day_not_closed",
            f"день {date_iso} участника {participant_id!r} не закрыт — дайджест строится по закрытым дням",
        )

    flags = sorted(
        sess.scalars(select(models.Flag.code).where(models.Flag.day_id == day_id)).all()
    )

    adj_rows = sess.scalars(
        select(models.Adjustment).where(models.Adjustment.day_id == day_id)
    ).all()
    adjustments = [
        {
            "adjustment_id": a.adjustment_id,
            "delta_cents": a.delta_cents,
            "reason": a.reason,
            "approved_by": a.approved_by,
            "created_at": a.created_at,
        }
        for a in adj_rows
    ]
    delta_cents = sum(a.delta_cents for a in adj_rows)

    disputes = [
        {"dispute_id": d.dispute_id, "status": d.status, "opened_by": d.opened_by,
         "reason": d.reason, "interval_refs": d.interval_refs or []}
        for d in sess.scalars(
            select(models.Dispute)
            .where(models.Dispute.day_id == day_id)
            .order_by(models.Dispute.opened_at)
        ).all()
    ]
    disputed_intervals = sorted({i for d in disputes for i in d["interval_refs"]})

    # Часы по классам: пересборка из БД (детерминированно) через golden-калькулятор
    computed = service.compute_day(sess, project, participant_id, date_iso)
    if computed["paid_seconds"] != rec.paid_seconds or computed["amount_cents"] != rec.amount_cents:
        raise DigestError(
            "day_mismatch",
            "пересчет дня расходится с закрытой записью — проверьте целостность БД",
        )

    amount_cents = rec.amount_cents + delta_cents
    return {
        "kind": "day_digest",
        "participant_id": participant_id,
        "project_id": project.project_id,
        "date": date_iso,
        "tz": rec.tz,
        "hours_by_class": {cls: computed["breakdown"].get(cls, "0") for cls in calc.PAID_CLASSES},
        "paid_seconds": rec.paid_seconds,
        "paid_hours": str(Decimal(rec.paid_seconds) / calc.H),
        "amount_cents": rec.amount_cents,
        "amount_rub": str(Decimal(rec.amount_cents) / 100),
        "adjustments": adjustments,
        "adjustments_delta_cents": delta_cents,
        "effective_amount_cents": amount_cents,
        "effective_amount_rub": str(Decimal(amount_cents) / 100),
        "cash_cents": rec.cash_cents,
        "equity_cents": rec.equity_cents,
        "share_bp": rec.share_bp,
        "share_pct": str(Decimal(rec.share_bp) / 100),
        "flags": flags,
        "disputes": disputes,
        "disputed_intervals": disputed_intervals,
        "rate_version": rec.rate_version,
        "policy_version": rec.policy_version,
        "sha256": rec.sha256_input,
    }


def render_day_digest(d: dict) -> str:
    """Детерминированный текст дайджеста дня (утвержденный формат — D6)."""
    cls_names = {
        calc.CLASS_ACTIVE: "человек",
        calc.CLASS_SUPERVISED: "агент под наблюдением",
        calc.CLASS_AUTONOMOUS: "автономный агент",
    }
    lines = [
        f"Дайджест дня {d['date']} — {d['participant_id']} (проект {d['project_id']}, {d['tz']})",
        f"Оплачиваемое время: {d['paid_hours']} ч",
    ]
    for cls in calc.PAID_CLASSES:
        lines.append(f"  {cls_names[cls]} ({cls}): {d['hours_by_class'].get(cls, '0')} ч")
    lines.append(f"Начислено: {d['amount_rub']} ₽ (cash {str(Decimal(d['cash_cents']) / 100)} ₽ / доли {str(Decimal(d['equity_cents']) / 100)} ₽)")
    lines.append(f"Доля дня: {d['share_pct']} % ({d['share_bp']} бп)")
    if d["adjustments"]:
        lines.append(f"Корректировки: {d['adjustments_delta_cents'] / 100:+.2f} ₽ → итог {d['effective_amount_rub']} ₽")
    if d["flags"]:
        lines.append(f"Флаги: {', '.join(d['flags'])}")
    if d["disputed_intervals"]:
        lines.append(f"Спорные интервалы: {', '.join(d['disputed_intervals'])} (спор: {d['disputes'][0]['dispute_id'] if d['disputes'] else ''}, статус {d['disputes'][0]['status'] if d['disputes'] else ''})")
    lines.append(f"Версии: ставка r{d['rate_version']}, политика v{d['policy_version']}; sha256 {d['sha256'][:12]}…")
    return "\n".join(lines)


# ---------- месячный дайджест (M6.2) ----------


def _month_unclosed_days(
    sess: Session, project: models.Project, participant_id: str, month: str, tz: ZoneInfo
) -> list[str]:
    """Дни месяца с интервалами, но без закрытой записи (pending-статус)."""
    rows = sess.execute(
        select(models.Interval.start_utc, models.Interval.end_utc)
        .join(models.Session, models.Interval.session_id == models.Session.session_id)
        .where(models.Session.participant_id == participant_id)
        .where(models.Session.project_id == project.project_id)
    ).all()
    dates = set()
    for start_s, end_s in rows:
        start = service.parse_utc(start_s).astimezone(tz)
        end = service.parse_utc(end_s).astimezone(tz)
        cur = start
        while cur < end:
            if cur.date().isoformat().startswith(month):
                dates.add(cur.date().isoformat())
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    closed = {
        r.date
        for r in sess.scalars(
            select(models.DayRecord)
            .where(models.DayRecord.participant_id == participant_id)
            .where(models.DayRecord.date.like(f"{month}%"))
            .where(models.DayRecord.closed == 1)
        ).all()
    }
    return sorted(dates - closed)


def month_digest(sess: Session, project: models.Project, participant_id: str, month: str) -> dict:
    """Итог месяца: оплачиваемые часы, вклад ₽, доли, остаток пула, pending."""
    if not (len(month) == 7 and month[4] == "-"):
        raise DigestError("bad_month", f"ожидается формат YYYY-MM, получено {month!r}")
    rows = sess.scalars(
        select(models.DayRecord)
        .where(models.DayRecord.participant_id == participant_id)
        .where(models.DayRecord.date.like(f"{month}%"))
        .where(models.DayRecord.closed == 1)
        .order_by(models.DayRecord.date)
    ).all()
    if not rows:
        raise DigestError(
            "no_days",
            f"за {month} у участника {participant_id!r} нет закрытых дней",
        )

    paid_seconds = sum(r.paid_seconds for r in rows)
    amount_cents = sum(r.amount_cents for r in rows)
    cash_cents = sum(r.cash_cents for r in rows)
    equity_cents = sum(r.equity_cents for r in rows)
    share_bp_month = sum(Decimal(r.share_bp) for r in rows).quantize(Decimal("0.0001"))

    day_ids = [r.day_id for r in rows]
    adj_rows = sess.scalars(
        select(models.Adjustment).where(models.Adjustment.day_id.in_(day_ids))
    ).all()
    delta_cents = sum(a.delta_cents for a in adj_rows)

    open_disputes = sess.scalars(
        select(models.Dispute)
        .where(models.Dispute.participant_id == participant_id)
        .where(models.Dispute.status == "open")
        .where(models.Dispute.day_id.like(f"{participant_id}|{month}%"))
    ).all()

    # Остаток пула: пул долей (по умолчанию 30% оценки = 3000 бп) минус
    # начисленные всем участникам бп этого месяца.
    _, _, params = service.load_policy(sess, project.project_id)
    pool_pct = Decimal(str(params.get("equity_pool_pct", DEFAULT_POOL_PCT)))
    pool_bp = (pool_pct * Decimal(100)).quantize(Decimal("0.0001"))
    accrued_all_bp = sum(
        Decimal(r.share_bp or 0)
        for r in sess.scalars(
            select(models.DayRecord).where(models.DayRecord.date.like(f"{month}%"))
        ).all()
    ).quantize(Decimal("0.0001"))
    remainder_bp = (pool_bp - accrued_all_bp).quantize(Decimal("0.0001"))

    tz_name = rows[-1].tz
    pending = {
        "open_disputes": [d.dispute_id for d in open_disputes],
        "unclosed_days": _month_unclosed_days(
            sess, project, participant_id, month, ZoneInfo(tz_name)
        ),
    }

    return {
        "kind": "month_digest",
        "participant_id": participant_id,
        "project_id": project.project_id,
        "month": month,
        "days": len(rows),
        "paid_seconds": paid_seconds,
        "paid_hours": str(Decimal(paid_seconds) / calc.H),
        "amount_cents": amount_cents,
        "amount_rub": str(Decimal(amount_cents) / 100),
        "adjustments_delta_cents": delta_cents,
        "effective_amount_rub": str(Decimal(amount_cents + delta_cents) / 100),
        "cash_cents": cash_cents,
        "equity_cents": equity_cents,
        "share_bp": str(share_bp_month),
        "share_pct": str((share_bp_month / 100).quantize(Decimal("0.0001"))),
        "pool_bp": str(pool_bp),
        "accrued_bp_month": str(accrued_all_bp),
        "pool_remainder_bp": str(remainder_bp),
        "pending": pending,
        "days_detail": [
            {"date": r.date, "paid_seconds": r.paid_seconds,
             "amount_cents": r.amount_cents, "share_bp": r.share_bp}
            for r in rows
        ],
    }


def render_month_digest(m: dict) -> str:
    """Детерминированный текст месячного дайджеста (формат D6)."""
    lines = [
        f"Итог месяца {m['month']} — {m['participant_id']} (проект {m['project_id']})",
        f"Закрыто дней: {m['days']}; оплачиваемое время: {m['paid_hours']} ч",
        f"Вклад: {m['amount_rub']} ₽" + (
            f" (с корректировками {m['effective_amount_rub']} ₽)"
            if m["adjustments_delta_cents"]
            else ""
        ),
        f"Начисленные доли: {m['share_pct']} % ({m['share_bp']} бп)",
        f"Пул долей: {m['pool_bp']} бп, начислено за месяц всем: {m['accrued_bp_month']} бп, остаток: {m['pool_remainder_bp']} бп",
        f"Ожидают: споры {len(m['pending']['open_disputes'])}, незакрытых дней {len(m['pending']['unclosed_days'])}",
    ]
    if m["pending"]["open_disputes"]:
        lines.append(f"  открытые споры: {', '.join(m['pending']['open_disputes'])}")
    if m["pending"]["unclosed_days"]:
        lines.append(f"  незакрытые дни: {', '.join(m['pending']['unclosed_days'])}")
    return "\n".join(lines)
