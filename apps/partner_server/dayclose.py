"""Джоба закрытия дня 00:00 пояса проекта (ADR-0017) + догоняющий прогон.

Принцип (development-plan.md §риск «простой закрытия дня»): незакрытые дни
закрываются при следующем запуске в порядке дат — close_catch_up().
Планировщик спит до ближайшей полуночи пояса каждого проекта и закрывает
предыдущий день для всех участников с интервалами.

M6: после закрытия — повтор недоставленных дайджестов (notify.retry_pending),
истечение просроченных споров (disputes.expire_overdue), а 1-го числа —
месячные дайджесты за предыдущий месяц (notify.dispatch_monthly).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from . import models, service

log = logging.getLogger("partner.dayclose")


def close_catch_up(sess: OrmSession, project: models.Project, today_local_date: str) -> list[str]:
    """Догоняющее закрытие: все незакрытые дни проекта с интервалами до сегодня."""
    tz = ZoneInfo(project.timezone)
    closed: list[str] = []

    participant_ids = sess.scalars(
        select(models.Session.participant_id)
        .where(models.Session.project_id == project.project_id)
        .distinct()
    ).all()

    for participant_id in sorted(participant_ids):
        rows = sess.scalars(
            select(models.Interval).join(
                models.Session,
                models.Interval.session_id == models.Session.session_id,
            )
            .where(models.Session.participant_id == participant_id)
            .where(models.Session.project_id == project.project_id)
        ).all()
        dates = set()
        for iv in rows:
            start = service.parse_utc(iv.start_utc).astimezone(tz)
            end = service.parse_utc(iv.end_utc).astimezone(tz)
            if start.date().isoformat() < today_local_date:
                dates.add(start.date().isoformat())
            if end.date().isoformat() < today_local_date:
                dates.add(end.date().isoformat())
        for date_iso in sorted(dates):
            rec = service.find_day_record(sess, project, participant_id, date_iso)
            if rec is not None and rec.closed:
                continue
            try:
                service.close_day(sess, project, participant_id, date_iso)
                closed.append(f"{project.project_id}|{participant_id}|{date_iso}")
            except service.DomainError as err:
                log.warning("не удалось закрыть %s|%s: %s", participant_id, date_iso, err.code)
    return closed


def _seconds_until_next_midnight(tz: ZoneInfo) -> float:
    now = datetime.now(tz)
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1.0, (next_midnight - now).total_seconds())


def run_scheduler(get_session, stop_event: threading.Event) -> None:
    """Цикл планировщика: закрывает предыдущий день в 00:00 пояса проекта."""
    while not stop_event.is_set():
        projects = get_session().query(models.Project).all()
        if not projects:
            if stop_event.wait(60):
                break
            continue
        # ближайшая полночь среди проектов
        waits = [
            _seconds_until_next_midnight(ZoneInfo(p.timezone)) for p in projects
        ]
        wait = min(waits)
        if stop_event.wait(wait):
            break
        now_utc = datetime.now(timezone.utc)
        for project in projects:
            tz = ZoneInfo(project.timezone)
            with get_session() as sess:
                closed = close_catch_up(sess, project, now_utc.astimezone(tz).date().isoformat())
                if closed:
                    log.info("закрыты дни проекта %s: %s", project.project_id, closed)
                _m6_jobs(sess, project)
        # защита от повторного срабатывания в ту же полночь
        if datetime.now(timezone.utc) < now_utc + timedelta(seconds=1):
            time.sleep(1)


def _m6_jobs(sess, project) -> None:
    """M6-обслуживание после закрытия дня: ретраи доставки, споры, месяц."""
    from datetime import datetime as dt

    from . import disputes, notify

    try:
        result = notify.retry_pending(sess)
        if result["pending"]:
            log.info("доставка дайджестов %s: %s/%s", project.project_id, result["sent"], result["pending"])
        expired = disputes.expire_overdue(sess)
        if expired:
            log.info("споры истекли: %s", expired)
        now_local = dt.now(ZoneInfo(project.timezone))
        if now_local.day == 1:
            prev_month = (now_local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            ids = notify.dispatch_monthly(sess, project, prev_month)
            log.info("месячные дайджесты %s за %s: %s", project.project_id, prev_month, ids)
    except Exception:  # noqa: BLE001 — M6-задачи не ломают закрытие дня
        log.exception("M6-задачи проекта %s завершились с ошибкой", project.project_id)


def start_scheduler_thread(get_session) -> threading.Thread:
    stop_event = threading.Event()

    def _run():
        try:
            run_scheduler(get_session, stop_event)
        except Exception:  # noqa: BLE001 — планировщик не должен ронять процесс
            log.exception("планировщик закрытия дня остановился с ошибкой")

    thread = threading.Thread(target=_run, name="partner-dayclose", daemon=True)
    thread.start()
    return thread
