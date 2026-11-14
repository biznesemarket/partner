"""Спор дня (M6.4, development-plan.md §6.4, time-accounting.md §спор).

Цикл по документации:
1. Разработчик помечает спорный день (опционально — конкретные interval_id):
   POST /v1/days/{date}/disputes или CLI `partner dispute <date>`.
   Открытый спор создается только по закрытому дню; один открытый спор на день.
2. Окно 3 дня (PARTNER_DISPUTE_DAYS): после дедлайна спор помечается
   expired и решение невозможно.
3. Директор решает: approved (корректировка delta_cents → adjustment,
   append-only, со ссылкой на day_record) или rejected.
4. Все шаги — в аудите (M5.4) и в очереди уведомлений (M6.3).
День никогда не переписывается: эффективная сумма = amount_cents +
Σ adjustments.delta_cents (см. GET /v1/days/{date} и дайджест).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, models, notify
from .config import settings


class DisputeError(Exception):
    """Ожидаемая ошибка спора → HTTP 4xx с кодом."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def open_dispute(
    sess: Session,
    project: models.Project,
    participant_id: str,
    date_iso: str,
    *,
    opened_by: str,
    reason: str,
    interval_ids: list[str] | None = None,
) -> dict:
    """Шаг 1: разработчик открывает спор по закрытому дню."""
    day_id = f"{participant_id}|{date_iso}"
    rec = sess.get(models.DayRecord, day_id)
    if rec is None or not rec.closed:
        raise DisputeError("day_not_closed", "спор открывается только по закрытому дню")

    existing = sess.scalars(
        select(models.Dispute)
        .where(models.Dispute.day_id == day_id)
        .where(models.Dispute.status == "open")
    ).first()
    if existing is not None:
        raise DisputeError(
            "dispute_already_open", f"по дню {day_id} уже есть открытый спор {existing.dispute_id}"
        )

    # Пометка интервалов: проверяем принадлежность участнику/проекту
    interval_ids = interval_ids or []
    if interval_ids:
        known = set(
            sess.scalars(
                select(models.Interval.interval_id)
                .join(models.Session, models.Interval.session_id == models.Session.session_id)
                .where(models.Session.participant_id == participant_id)
                .where(models.Session.project_id == project.project_id)
            ).all()
        )
        unknown = [i for i in interval_ids if i not in known]
        if unknown:
            raise DisputeError(
                "unknown_intervals",
                f"интервалы не принадлежат участнику: {', '.join(unknown)}",
            )

    now = _now()
    deadline = now + timedelta(days=settings.dispute_window_days)
    dispute_id = f"dsp-{uuid.uuid4().hex[:12]}"
    sess.add(
        models.Dispute(
            dispute_id=dispute_id,
            day_id=day_id,
            participant_id=participant_id,
            project_id=project.project_id,
            status="open",
            opened_by=opened_by,
            opened_at=_iso(now),
            window_deadline=_iso(deadline),
            interval_refs=interval_ids,
            reason=reason,
        )
    )
    sess.commit()
    audit.record(
        sess,
        actor=opened_by,
        entity="disputes",
        entity_id=dispute_id,
        action="dispute.opened",
        new_value={"day_id": day_id, "reason": reason, "interval_refs": interval_ids},
        meta={"window_deadline": _iso(deadline)},
    )
    ntf_id = notify.queue(
        sess,
        kind="dispute_opened",
        recipient="director",
        payload={
            "dispute_id": dispute_id,
            "day_id": day_id,
            "reason": reason,
            "interval_refs": interval_ids,
            "window_deadline": _iso(deadline),
        },
        text=(
            f"Открыт спор {dispute_id} по дню {date_iso} ({participant_id}). "
            f"Причина: {reason}. Решение до {_iso(deadline)} (окно {settings.dispute_window_days} дн.)."
        ),
    )
    # директору важно узнать сразу: доставка сейчас, при недоставке — ретраи
    notify.deliver(sess, sess.get(models.Notification, ntf_id))
    return {
        "status": "created",
        "dispute_id": dispute_id,
        "day_id": day_id,
        "window_deadline": _iso(deadline),
    }


def resolve_dispute(
    sess: Session,
    dispute_id: str,
    *,
    decided_by: str,
    decision: str,
    delta_cents: int | None = None,
    resolution: str | None = None,
) -> dict:
    """Шаги 2–3: решение директора. approved → adjustment (append-only)."""
    if decision not in ("approved", "rejected"):
        raise DisputeError("bad_decision", "decision должен быть approved или rejected")

    dispute = sess.get(models.Dispute, dispute_id)
    if dispute is None:
        raise DisputeError("no_dispute", f"спор {dispute_id!r} не найден")
    if dispute.status != "open":
        raise DisputeError("dispute_not_open", f"спор {dispute_id} в статусе {dispute.status}")

    now = _now()
    if now > datetime.fromisoformat(dispute.window_deadline):
        dispute.status = "expired"
        sess.commit()
        audit.incident(
            sess,
            code="dispute_window_expired",
            entity="disputes",
            entity_id=dispute_id,
            meta={"window_deadline": dispute.window_deadline},
        )
        raise DisputeError(
            "dispute_window_expired",
            f"окно спора истекло {dispute.window_deadline} (3 дня, time-accounting.md §спор)",
        )

    if decision == "approved":
        if delta_cents is None or delta_cents == 0:
            raise DisputeError(
                "delta_required",
                "для approved требуется ненулевая delta_cents (корректировка закрытого дня)",
            )
        adjustment_id = f"adj-{uuid.uuid4().hex[:12]}"
        sess.add(
            models.Adjustment(
                adjustment_id=adjustment_id,
                day_id=dispute.day_id,
                delta_cents=delta_cents,
                reason=f"спор {dispute_id}: {resolution or dispute.reason}",
                approved_by=decided_by,
                created_at=_iso(now),
            )
        )

    dispute.status = "resolved" if decision == "approved" else "rejected"
    dispute.decided_by = decided_by
    dispute.decided_at = _iso(now)
    dispute.resolution = resolution or ""
    sess.commit()

    audit.record(
        sess,
        actor=decided_by,
        entity="disputes",
        entity_id=dispute_id,
        action="dispute.resolved",
        old_value={"status": "open"},
        new_value={"status": dispute.status, "delta_cents": delta_cents},
        meta={"day_id": dispute.day_id, "resolution": dispute.resolution},
    )
    ntf_id = notify.queue(
        sess,
        kind="dispute_resolved",
        recipient=dispute.participant_id,
        payload={
            "dispute_id": dispute_id,
            "day_id": dispute.day_id,
            "status": dispute.status,
            "delta_cents": delta_cents,
        },
        text=(
            f"Спор {dispute_id} по дню {dispute.day_id.split('|')[1]} решен: {dispute.status}"
            + (f", корректировка {delta_cents / 100:+.2f} ₽ (adjustment {adjustment_id})" if decision == "approved" else "")
            + f". Решение: {decided_by}."
        ),
    )
    notify.deliver(sess, sess.get(models.Notification, ntf_id))
    return {
        "status": dispute.status,
        "dispute_id": dispute_id,
        "day_id": dispute.day_id,
        "adjustment_id": adjustment_id if decision == "approved" else None,
    }


def expire_overdue(sess: Session) -> list[str]:
    """Шаг 2 (джоба): открытые споры с истекшим окном → expired."""
    now = _iso(_now())
    overdue = sess.scalars(
        select(models.Dispute)
        .where(models.Dispute.status == "open")
        .where(models.Dispute.window_deadline < now)
    ).all()
    expired: list[str] = []
    for d in overdue:
        d.status = "expired"
        expired.append(d.dispute_id)
    if expired:
        sess.commit()
        audit.incident(
            sess,
            code="dispute_window_expired",
            entity="disputes",
            entity_id=",".join(expired),
            meta={"auto": True},
        )
    return expired


def list_disputes(sess: Session, status: str | None = None, limit: int = 100) -> list[models.Dispute]:
    stmt = select(models.Dispute).order_by(models.Dispute.opened_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(models.Dispute.status == status)
    return sess.scalars(stmt).all()
