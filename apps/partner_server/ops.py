"""M8.4: эксплуатационные метрики закрытия дня и доставки.

Метрики считаются из БД (никакого собственного состояния — источник истины
один) и отдаются в формате Prometheus text на GET /v1/metrics. В метриках
нет персональных данных — только агрегаты, поэтому эндпоинт открыт без
Bearer-токена (для скрейпа Prometheus), но содержит только агрегаты.

Перечень (development-plan.md M8.4: heartbeats/min, задержка закрытия дня,
ошибки доставки дайджестов; алерты — docs/07-ops/runbook.md §алерты):

- partner_heartbeats_total          — всего принято heartbeats
- partner_heartbeats_last_hour      — принято за последний час (≈ в минуту / 60)
- partner_intervals_total           — union-интервалов собрано
- partner_days_closed_total         — закрытых day_records
- partner_day_close_delay_seconds   — задержка закрытия последнего дня
                                      (closed_at − 00:00 следующего дня, сек)
- partner_days_open_overdue         — незакрытых дней старше грейса (алерт!)
- partner_digest_pending            — уведомлений в очереди
- partner_digest_failed             — недоставленных после N попыток (алерт!)
- partner_disputes_open             — открытых споров дня

Сравнения времени — только через parse ISO и datetime, никогда по строке:
в received_at/end_utc хранятся ISO со смещением пояса источника.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from . import models
from .config import settings


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def collect(sess: OrmSession) -> dict[str, float]:
    """Собрать все метрики одним вызовом (агрегаты БД + разбор в Python)."""
    metrics: dict[str, float] = {}
    now_utc = datetime.now(timezone.utc)

    metrics["partner_heartbeats_total"] = sess.scalar(
        select(func.count()).select_from(models.Heartbeat)
    ) or 0

    # ≈ heartbeats/min: за последний час, фильтр в Python (offset-безопасно).
    hour_ago = now_utc - timedelta(hours=1)
    received = sess.scalars(select(models.Heartbeat.received_at)).all()
    metrics["partner_heartbeats_last_hour"] = sum(
        1
        for raw in received
        if (dt := _parse_iso(raw)) is not None and dt >= hour_ago
    )

    metrics["partner_intervals_total"] = sess.scalar(
        select(func.count()).select_from(models.Interval)
    ) or 0

    metrics["partner_days_closed_total"] = sess.scalar(
        select(func.count())
        .select_from(models.DayRecord)
        .where(models.DayRecord.closed == 1)
    ) or 0

    # Задержка закрытия последнего дня: closed_at − 00:00 следующего дня
    # в поясе записи. closed_at NULL (дни, закрытые до M8) — пропускаются.
    last = sess.execute(
        select(models.DayRecord)
        .where(models.DayRecord.closed == 1)
        .where(models.DayRecord.closed_at.is_not(None))
        .order_by(models.DayRecord.closed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is not None:
        closed_at = _parse_iso(last.closed_at)
        midnight = datetime.combine(
            datetime.fromisoformat(last.date).date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=ZoneInfo(last.tz),
        )
        if closed_at is not None:
            delay = (closed_at - midnight).total_seconds()
            # отрицательная задержка — часы записей: закрытие не опоздало
            metrics["partner_day_close_delay_seconds"] = max(0.0, delay)

    # Незакрытые дни с интервалами старше грейса — алерт (runbook §алерты).
    # День = participant|date в поясе проекта (тот же day_id, что в
    # day_records). MVP-масштаб (десятки участников) — считаем в Python.
    grace_deadline = now_utc - timedelta(hours=settings.grace_hours)
    rows = sess.execute(
        select(
            models.Interval.end_utc,
            models.Session.participant_id,
            models.Project.timezone,
        )
        .join(models.Session, models.Interval.session_id == models.Session.session_id)
        .join(models.Project, models.Session.project_id == models.Project.project_id)
        .distinct()
    ).all()
    overdue_days: set[str] = set()
    tz_cache: dict[str, ZoneInfo] = {}
    # Аудит-фикс F8 (P2): дедупликация (participant, дата) ДО sess.get —
    # раньше get выполнялся на каждую строку интервалов (N+1 по журналу).
    day_candidates: set[tuple[str, str, str]] = set()
    for end_raw, participant_id, project_tz in rows:
        end_dt = _parse_iso(end_raw)
        if end_dt is None or end_dt >= grace_deadline:
            continue
        tz = tz_cache.setdefault(project_tz, ZoneInfo(project_tz))
        day_candidates.add((participant_id, project_tz, end_dt.astimezone(tz).date().isoformat()))
    for participant_id, project_tz, date_iso in day_candidates:
        day_id = f"{participant_id}|{date_iso}"
        if sess.get(models.DayRecord, day_id) is None:
            overdue_days.add(day_id)
    metrics["partner_days_open_overdue"] = len(overdue_days)


    metrics["partner_digest_pending"] = sess.scalar(
        select(func.count())
        .select_from(models.Notification)
        .where(models.Notification.status == "pending")
    ) or 0
    metrics["partner_digest_failed"] = sess.scalar(
        select(func.count())
        .select_from(models.Notification)
        .where(models.Notification.status == "failed")
    ) or 0
    metrics["partner_disputes_open"] = sess.scalar(
        select(func.count())
        .select_from(models.Dispute)
        .where(models.Dispute.status == "open")
    ) or 0

    return metrics


def render(metrics: dict[str, float]) -> str:
    """Prometheus text format: HELP/TYPE для алертящих gauge + значения."""
    gauges = {
        "partner_heartbeats_last_hour",
        "partner_day_close_delay_seconds",
        "partner_days_open_overdue",
        "partner_digest_pending",
        "partner_digest_failed",
        "partner_disputes_open",
        "partner_license_days_left",
    }
    lines: list[str] = []
    for name in sorted(metrics):
        kind = "gauge" if name in gauges else "counter"
        lines.append(f"# TYPE {name} {kind}")
        lines.append(f"{name} {metrics[name]:g}")
    return "\n".join(lines) + "\n"
