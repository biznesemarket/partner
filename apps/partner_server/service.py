"""Сервисный слой M4–M6: прием heartbeat, сборка/закрытие дня, дайджест-хук.

Golden-parity (M4 DoD): числа дней считаются ТОЛЬКО через partner_cli.calc —
той же нормативной реализацией, что закреплена тестами T-01..T-12.
Сервер лишь превращает записи БД в Interval и вызывает calc.calc_day.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from partner_cli import calc

from . import antifraud
from . import audit as audit_log
from . import connectors
from . import models
from .config import settings

VALID_CLASSES = {
    calc.CLASS_ACTIVE,
    calc.CLASS_SUPERVISED,
    calc.CLASS_AUTONOMOUS,
    calc.CLASS_IDLE,
}

log = logging.getLogger("partner.service")


class DomainError(Exception):
    """Ожидаемая ошибка домена → HTTP 4xx с осмысленным текстом.

    status — явный HTTP-статус (например 402 для лицензионного отказа);
    по умолчанию статус вычисляется по коду в routes.domain_error_status.
    """

    def __init__(self, code: str, message: str, status: int | None = None):
        self.code, self.message = code, message
        self.status = status
        super().__init__(f"{code}: {message}")


def _license_guard() -> None:
    """M9.1: лицензионный гейт перед закрытием дня (licensing.py).

    invalid/expired → DomainError license_invalid/license_expired (HTTP 402);
    demo/active/grace — разрешено. Импорт локальный: licensing не зависит
    от service, а service не должен тянуть его на каждый вызов.
    """
    from . import licensing
    from .config import settings

    st = licensing.current_state(settings)
    if st.state in ("demo", "active", "grace"):
        return
    raise DomainError(
        f"license_{st.state}",
        f"закрытие дня запрещено состоянием лицензии ({st.state}); "
        "чтение и экспорт доступны — данные клиента принадлежат клиенту",
        status=402,
    )


# ---------- время ----------


def parse_utc(s: str) -> datetime:
    """ISO 8601 → aware datetime UTC. Поддержка суффикса Z и смещений."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise DomainError("bad_datetime", f"ожидается aware ISO 8601 UTC: {s!r}")
    return dt.astimezone(timezone.utc)


def local_pieces(start_utc: datetime, end_utc: datetime, tz: ZoneInfo):
    """Разрез интервала [start,end) на локальные (дата, секунда_от_00:00).

    Интервал, пересекающий полночь пояса проекта, попадает в два дня (T-05).
    """
    cur = start_utc.astimezone(tz)
    stop = end_utc.astimezone(tz)
    while cur < stop:
        day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        seg_end = min(stop, day_end)
        start_s = int((cur - day_start).total_seconds())
        end_s = start_s + int((seg_end - cur).total_seconds())
        yield day_start.date().isoformat(), start_s, end_s
        cur = seg_end


# ---------- политика ----------


def load_policy(
    sess: Session, project_id: str
) -> tuple[models.Policy, calc.Policy, dict]:
    """Активная политика проекта: calc.Policy для golden-калькулятора + params."""
    pol = sess.scalars(
        select(models.Policy)
        .where(models.Policy.project_id == project_id)
        .order_by(models.Policy.policy_version.desc())
        .limit(1)
    ).first()
    if pol is None:
        raise DomainError("no_policy", f"для проекта {project_id!r} нет политики")
    params = dict(pol.params_json or {})
    calc_pol = calc.Policy(
        rate_rub_per_hour=int(params.get("rate_rub_per_hour", 500)),
        valuation_rub=int(params.get("valuation_rub", 10_000_000)),
        daily_cap_hours=float(params.get("daily_cap_hours", 12.0)),
        autonomous_paid_cap_hours=float(
            params.get("autonomous_paid_cap_hours", 2.0)
        ),
        k_supervised=float(params.get("k_supervised", 1.0)),
        k_autonomous=float(params.get("k_autonomous", 0.2)),
        manual_only_policy=str(params.get("manual_only_policy", "track_only")),
        cash_share_pct=float(params.get("cash_share_pct", 0.0)),
    )
    return pol, calc_pol, params


def rate_version_for(sess: Session, policy_id: str, date_iso: str):
    """Версия ставки, действующая на дату; задним числом не ставится (V-02)."""
    rows = sess.scalars(
        select(models.RateVersion)
        .where(models.RateVersion.policy_id == policy_id)
        .where(models.RateVersion.effective_from <= date_iso)
        .order_by(models.RateVersion.effective_from.desc())
        .limit(1)
    ).first()
    return rows


# ---------- изоляция tenants (threat-model §4: deny by default) ----------


def ensure_same_tenant(project: models.Project, participant: models.Participant) -> None:
    """Кросс-тенантный запрос запрещен: 403 + инцидент в аудите."""
    if participant.tenant_id != project.tenant_id:
        raise DomainError("cross_tenant_denied", "участник и проект принадлежат разным организациям")


# ---------- прием heartbeat ----------


def ingest_heartbeat(
    sess: Session,
    *,
    event_id: str,
    session_id: str,
    seq: int,
    cls: str,
    start_utc: str,
    end_utc: str,
    received_at: datetime,
    signals: dict | None = None,
) -> dict:
    """Идемпотентный прием: повтор event_id возвращает duplicate без записи.

    Сервер не суммирует перекрывающиеся интервалы — union делается при сборке
    дня (T-04, R3). Пустые/обращенные интервалы отклоняются.
    """
    if cls not in VALID_CLASSES:
        raise DomainError("bad_class", f"класс {cls!r} не входит в {sorted(VALID_CLASSES)}")
    start = parse_utc(start_utc)
    end = parse_utc(end_utc)
    if end <= start:
        raise DomainError("bad_interval", "end_utc должен быть позже start_utc")

    existing = sess.get(models.Heartbeat, event_id)
    if existing is not None:
        # T-3 (threat-model): replay — повтор не записывается, инцидент в журнале
        session = sess.get(models.Session, session_id)
        project = (
            sess.get(models.Project, session.project_id) if session is not None else None
        )
        audit_log.incident(
            sess,
            code="replay",
            entity="heartbeats",
            entity_id=event_id,
            meta={"session_id": session_id, "seq": seq},
            tenant_id=project.tenant_id if project is not None else None,
            project_id=project.project_id if project is not None else None,
        )
        return {"status": "duplicate", "event_id": event_id}

    session = sess.get(models.Session, session_id)
    if session is None:
        raise DomainError("no_session", f"сессия {session_id!r} не найдена")

    dup_seq = sess.scalars(
        select(models.Heartbeat).where(
            models.Heartbeat.session_id == session_id,
            models.Heartbeat.seq == seq,
        )
    ).first()
    if dup_seq is not None:
        project = sess.get(models.Project, session.project_id)
        audit_log.incident(
            sess,
            code="duplicate_seq",
            entity="heartbeats",
            entity_id=event_id,
            meta={"session_id": session_id, "seq": seq},
            tenant_id=project.tenant_id if project is not None else None,
            project_id=project.project_id if project is not None else None,
        )
        raise DomainError(
            "duplicate_seq", f"seq={seq} для сессии {session_id!r} уже записан"
        )

    sess.add(
        models.Heartbeat(
            event_id=event_id,
            session_id=session_id,
            seq=seq,
            received_at=received_at.isoformat(timespec="seconds"),
            signals=signals,
        )
    )
    sess.add(
        models.Interval(
            interval_id=event_id,
            session_id=session_id,
            cls=cls,
            start_utc=start.isoformat(timespec="seconds"),
            end_utc=end.isoformat(timespec="seconds"),
        )
    )
    sess.commit()
    return {"status": "accepted", "event_id": event_id}


# ---------- сборка дня ----------


def day_id_for(project: models.Project, participant_id: str, date_iso: str) -> str:
    """Идентификатор закрытого дня в разрезе проекта (v1.2).

    Исторические записи v1.0/v1.1 имели формат ``participant|date``; они
    продолжают читаться через find_day_record (обратная совместимость).
    """
    return f"{project.project_id}|{participant_id}|{date_iso}"


def find_day_record(
    sess: Session, project: models.Project, participant_id: str, date_iso: str
) -> models.DayRecord | None:
    """Найти день проекта: сначала новый project-scoped id, затем legacy.

    Legacy-запись другого проекта не возвращается: для второго проекта
    создается отдельная запись с project-scoped id (issue #4).
    """
    rec = sess.get(models.DayRecord, day_id_for(project, participant_id, date_iso))
    if rec is not None:
        return rec
    legacy = sess.get(
        models.DayRecord, f"{participant_id}|{date_iso}"
    )
    if legacy is not None and (
        legacy.project_id is None or legacy.project_id == project.project_id
    ):
        return legacy
    return None


def day_intervals(
    sess: Session, project: models.Project, participant_id: str, date_iso: str
) -> tuple[list[antifraud.Piece], list[str], bool]:
    """Фрагменты дня в секундах от 00:00 пояса проекта + флаги источника.

    Правила на стыке БД и калькулятора:
    - сессия без task_ref (no_task_link) → класс idle, не оплачивается;
    - сессия без агента → agent_attached=False (T-10, track_only);
    - пересечение сессий не суммируется — union в calc.calc_day (T-04);
    - heartbeat присоединяется по interval_id=event_id — детекты M5 видят
      received_at и signals.
    """
    tz = ZoneInfo(project.timezone)
    flags: list[str] = []
    # Локальная полночь date_iso в поясе проекта (аудит-фикс F4: раньше
    # сюда попадала полночь UTC, переведенная в пояс, — для поясов с
    # отрицательным смещением окно дня сдвигалось на сутки назад).
    day_start = datetime.fromisoformat(date_iso + "T00:00:00").replace(tzinfo=tz)
    # Полночь следующего дня — через дату (+1 день), а не timedelta: при
    # переходе на летнее/зимнее время сутки длятся 23/25 ч.
    next_date = (day_start + timedelta(days=1)).date()
    window_end = datetime.fromisoformat(next_date.isoformat() + "T00:00:00").replace(
        tzinfo=tz
    )
    # Аудит-фикс F4 (P2): нижняя граница окна. Раньше запрос грузил ВСЮ историю
    # интервалов участника (только верхняя граница), и compute_day замедлялся
    # линейно с ростом журнала, а close_catch_up — квадратично. Точная нижняя
    # граница — end_utc (не start_utc!): интервал попадает в день D
    # ⟺ он пересекает локальную полночь D, то есть
    # start_utc < полночь(D+1) И end_utc > полночь(D) — в UTC.
    # Это корректно и для интервалов, пересекающих полночь (T-05).
    lower_iso = day_start.astimezone(timezone.utc).isoformat(timespec="seconds")
    upper_iso = window_end.astimezone(timezone.utc).isoformat(timespec="seconds")

    rows = sess.execute(
        select(models.Interval, models.Session, models.Heartbeat)
        .join(models.Session, models.Interval.session_id == models.Session.session_id)
        .join(
            models.Heartbeat,
            models.Heartbeat.event_id == models.Interval.interval_id,
            isouter=True,
        )
        .where(models.Session.participant_id == participant_id)
        .where(models.Session.project_id == project.project_id)
        .where(models.Interval.end_utc > lower_iso)
        .where(models.Interval.start_utc < upper_iso)
    ).all()

    pieces: list[antifraud.Piece] = []
    agent_attached = True
    for iv, session, hb in rows:
        if session.task_ref is None:
            flags.append("no_task_link")
        if not session.agent_attached:
            agent_attached = False
        start = parse_utc(iv.start_utc)
        end = parse_utc(iv.end_utc)
        cls = calc.CLASS_IDLE if session.task_ref is None else iv.cls
        received = parse_utc(hb.received_at) if hb is not None else None
        for d, s, e in local_pieces(start, end, tz):
            if d == date_iso:
                pieces.append(
                    antifraud.Piece(
                        interval=calc.Interval(s, e, cls),
                        cls=cls,
                        start_dt=start,
                        end_dt=end,
                        signals=(hb.signals if hb is not None else None),
                        received_at=received,
                    )
                )
    flags = sorted(set(flags))
    if not agent_attached:
        flags.append("manual_track_only")
    return pieces, flags, agent_attached


def _day_window(project: models.Project, date_iso: str) -> tuple[str, str]:
    """UTC-границы локального дня проекта [00:00, 24:00)."""
    tz = ZoneInfo(project.timezone)
    day_start = datetime.fromisoformat(date_iso + "T00:00:00").replace(tzinfo=tz)
    next_date = (day_start + timedelta(days=1)).date()
    window_end = datetime.fromisoformat(next_date.isoformat() + "T00:00:00").replace(
        tzinfo=tz
    )
    return (
        day_start.astimezone(timezone.utc).isoformat(timespec="seconds"),
        window_end.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )


def _no_output_flag(
    sess: Session,
    project: models.Project,
    participant_id: str,
    date_iso: str,
    pieces: list[antifraud.Piece],
    paid_hours: Decimal,
) -> list[str]:
    """time-accounting.md §флаги: >3 оплач. ч при нуле коммитов/диффов/инструментов.

    Флаг не меняет начисление. Учитываются только факты, привязанные к
    ветке/задаче сессий этого участника, и tool_calls из сигналов heartbeat.
    """
    if paid_hours <= Decimal("3"):
        return []
    tool_calls = 0
    for piece in pieces:
        try:
            tool_calls += max(0, int((piece.signals or {}).get("tool_calls", 0) or 0))
        except (TypeError, ValueError):
            continue
    lower_iso, upper_iso = _day_window(project, date_iso)
    rows = sess.execute(
        select(models.Session.branch, models.Session.task_ref)
        .join(models.Interval, models.Interval.session_id == models.Session.session_id)
        .where(models.Session.participant_id == participant_id)
        .where(models.Session.project_id == project.project_id)
        .where(models.Interval.end_utc > lower_iso)
        .where(models.Interval.start_utc < upper_iso)
    ).all()
    branches = {row.branch for row in rows if row.branch}
    task_refs = {connectors.task_key(row.task_ref) for row in rows if row.task_ref}
    activity = connectors.output_activity(
        sess, project.project_id, lower_iso, upper_iso, branches, task_refs
    )
    outputs = tool_calls + activity["commits"] + activity["pull_requests"] + activity["diff_lines"]
    return [] if outputs else ["no_output"]


def compute_day(
    sess: Session,
    project: models.Project,
    participant_id: str,
    date_iso: str,
) -> dict:
    """Расчет дня без записи в БД (preview). Числа — из calc.calc_day (golden)."""
    pol, calc_pol, params = load_policy(sess, project.project_id)
    pieces, flags, agent_attached = day_intervals(
        sess, project, participant_id, date_iso
    )

    raw, fraud_flags = antifraud.apply(
        pieces,
        night_spike_hours=float(params.get("night_spike_hours", 6.0)),
        max_clock_skew_sec=float(params.get("max_clock_skew_sec", 120.0)),
        grace_hours=settings.grace_hours,
    )
    flags = flags + fraud_flags
    rv = rate_version_for(sess, pol.policy_id, date_iso)
    rate_slots = [(0, rv.value_cents // 100)] if rv else [(0, calc_pol.rate_rub_per_hour)]

    result = calc.calc_day(raw, calc_pol, rate_slots=rate_slots, agent_attached=agent_attached)
    flags = flags + _no_output_flag(
        sess, project, participant_id, date_iso, pieces, result.paid_hours
    )

    rate_version = 1 + len(
        [
            r
            for r in sess.scalars(
                select(models.RateVersion).where(
                    models.RateVersion.policy_id == pol.policy_id
                )
            )
            if r.effective_from <= date_iso
        ]
    )
    return {
        "participant_id": participant_id,
        "date": date_iso,
        "tz": project.timezone,
        "paid_hours": str(result.paid_hours),
        "paid_seconds": int(result.paid_hours * calc.H),
        "amount_cents": result.amount_cents,
        "amount_rub": str(result.amount_rub),
        "share_bp": str(result.share_bp),
        "share_pct": str(result.share_pct),
        "flags": sorted(set(flags + result.flags)),
        "breakdown": result.breakdown,
        "intervals_raw": len(raw),
        "rate_version": rate_version,
        "policy_version": pol.policy_version,
        "agent_attached": agent_attached,
        # Аудит-фикс F2 (P1): доля cash (R8) из политики — раньше close_day
        # всегда получал 0.0 и записывал весь день в equity_cents, игнорируя
        # cash_share_pct политики.
        "cash_share_pct": float(params.get("cash_share_pct", 0.0)),
    }


def _payload_sha(participant_id: str, date_iso: str, computed: dict) -> str:
    payload = json.dumps(
        {
            "participant": participant_id,
            "date": date_iso,
            "paid_seconds": computed["paid_seconds"],
            "amount_cents": computed["amount_cents"],
            "flags": computed["flags"],
            "breakdown": computed["breakdown"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recompute_adjustment_id(
    day_id: str, sha: str, effective_cents: int | None = None
) -> str:
    """Детерминированный id компенсации пересчета: идемпотентность (ADR-0021).

    Базовый ключ — day_id|sha. Если та же компенсация уже была, но база
    успела измениться (возврат данных после другой компенсации), добавляется
    суффикс эффективной суммы — отдельная обратная запись.
    """
    key = f"{day_id}|{sha}"
    if effective_cents is not None:
        key = f"{key}|{effective_cents}"
    return f"adj-recalc-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


def _compensate_reclose(
    sess: Session,
    project: models.Project,
    rec: models.DayRecord,
    computed: dict,
    sha: str,
) -> dict:
    """Пересчет закрытого дня: запись не меняется, разница — adjustment.

    ADR-0021: принятое начисление неизменяемо. Если поздние события в грейсе
    изменили расчет, создается компенсирующая запись на дельту (append-only);
    повторный пересчет с тем же sha идемпотентен по детерминированному id.
    """
    adjustments = sess.scalars(
        select(models.Adjustment).where(models.Adjustment.day_id == rec.day_id)
    ).all()
    effective_cents = rec.amount_cents + sum(a.delta_cents for a in adjustments)
    base_id = _recompute_adjustment_id(rec.day_id, sha)
    existing = sess.get(models.Adjustment, base_id)
    if existing is not None and computed["amount_cents"] == effective_cents:
        result = get_day(sess, project, rec.participant_id, rec.date)
        result.update(
            {
                "status": "duplicate_compensation",
                "adjustment_id": base_id,
                "delta_cents": existing.delta_cents,
                "recomputed_amount_cents": computed["amount_cents"],
                "recomputed_paid_seconds": computed["paid_seconds"],
                "recomputed_sha256": sha,
            }
        )
        return result

    if (
        existing is None
        and sha == rec.sha256_input
        and computed["amount_cents"] == effective_cents
    ):
        result = get_day(sess, project, rec.participant_id, rec.date)
        result.update(
            {
                "status": "duplicate",
                "sha256": rec.sha256_input,
                "recomputed_sha256": sha,
            }
        )
        return result

    # База изменилась после прежней компенсации (например, возврат данных):
    # отдельный id, чтобы append-only история сохранила обратную запись.
    adj_id = (
        base_id
        if existing is None
        else _recompute_adjustment_id(rec.day_id, sha, effective_cents)
    )
    already = sess.get(models.Adjustment, adj_id)
    if already is not None:
        result = get_day(sess, project, rec.participant_id, rec.date)
        result.update(
            {
                "status": "duplicate_compensation",
                "adjustment_id": adj_id,
                "delta_cents": already.delta_cents,
                "recomputed_amount_cents": computed["amount_cents"],
                "recomputed_paid_seconds": computed["paid_seconds"],
                "recomputed_sha256": sha,
            }
        )
        return result

    delta_cents = computed["amount_cents"] - effective_cents
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sess.add(
        models.Adjustment(
            adjustment_id=adj_id,
            day_id=rec.day_id,
            delta_cents=delta_cents,
            reason=(
                "автоматическая компенсация пересчета закрытого дня: "
                f"sha {rec.sha256_input[:12]}… → {sha[:12]}…, "
                f"часы {rec.paid_seconds} → {computed['paid_seconds']}"
            ),
            approved_by="system-late-events",
            created_at=now_iso,
        )
    )
    # Флаги только добавляются: исходные доказательства не удаляются.
    known_flags = {
        f.code
        for f in sess.scalars(
            select(models.Flag).where(models.Flag.day_id == rec.day_id)
        ).all()
    }
    for code in computed["flags"]:
        if code not in known_flags:
            sess.add(
                models.Flag(
                    day_id=rec.day_id,
                    code=code,
                    detail=computed["breakdown"].get("note"),
                )
            )
    sess.commit()
    audit_log.record(
        sess,
        actor="system-dayclose",
        entity="day_records",
        entity_id=rec.day_id,
        action="day.reclose_compensated",
        old_value={
            "sha256": rec.sha256_input,
            "paid_seconds": rec.paid_seconds,
            "amount_cents": rec.amount_cents,
            "effective_amount_cents": effective_cents,
        },
        new_value={
            "sha256": sha,
            "paid_seconds": computed["paid_seconds"],
            "amount_cents": computed["amount_cents"],
            "delta_cents": delta_cents,
            "adjustment_id": adj_id,
        },
        meta={"reason": "late_events_or_recompute", "day_id": rec.day_id},
        tenant_id=project.tenant_id,
        project_id=project.project_id,
    )
    result = get_day(sess, project, rec.participant_id, rec.date)
    result.update(
        {
            "status": "compensated",
            "adjustment_id": adj_id,
            "delta_cents": delta_cents,
            "recomputed_amount_cents": computed["amount_cents"],
            "recomputed_paid_seconds": computed["paid_seconds"],
            "recomputed_sha256": sha,
        }
    )
    return result


def close_day(
    sess: Session,
    project: models.Project,
    participant_id: str,
    date_iso: str,
) -> dict:
    """Закрытие дня (ADR-0017, ADR-0021): запись неизменяема после закрытия.

    Идемпотентный повтор с тем же расчетом возвращает duplicate. Если после
    закрытия пришли события в грейсе и пересчет изменился — создается
    компенсирующая запись (adjustment), а day_record не перезаписывается.
    """
    # M9.1: лицензия invalid/expired блокирует новые закрытия; чтение,
    # экспорт и снятие системы не блокируются («данные клиента — у клиента»).
    _license_guard()
    computed = compute_day(sess, project, participant_id, date_iso)
    rec = find_day_record(sess, project, participant_id, date_iso)
    day_id = rec.day_id if rec is not None else day_id_for(project, participant_id, date_iso)
    cash_cents, equity_cents = calc.cash_split(
        computed["amount_cents"],
        float(computed.get("cash_share_pct", 0.0) or 0.0)
        if "cash_share_pct" in computed
        else 0.0,
    )
    sha = _payload_sha(participant_id, date_iso, computed)

    if rec is not None and rec.project_id and rec.project_id != project.project_id:
        raise DomainError(
            "day_other_project",
            f"день {day_id} уже принадлежит проекту {rec.project_id!r}",
            status=409,
        )
    if rec is not None and rec.closed:
        return _compensate_reclose(sess, project, rec, computed, sha)
    if rec is None:
        rec = models.DayRecord(day_id=day_id)
        sess.add(rec)
    rec.participant_id = participant_id
    rec.project_id = project.project_id
    rec.date = date_iso
    rec.tz = computed["tz"]
    rec.paid_seconds = computed["paid_seconds"]
    rec.amount_cents = computed["amount_cents"]
    rec.cash_cents = cash_cents
    rec.equity_cents = equity_cents
    rec.share_bp = computed["share_bp"]
    rec.rate_version = computed["rate_version"]
    rec.valuation_version = 1
    rec.policy_version = computed["policy_version"]
    rec.sha256_input = sha
    rec.closed = 1
    # M8.4: момент закрытия (ISO UTC) — метрика задержки закрытия дня.
    # При повторном закрытии (идемпотентном) не перезаписываем первую запись:
    # closed_at = min(существующий, текущий).
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if rec.closed_at is None or now_iso < rec.closed_at:
        rec.closed_at = now_iso

    sess.query(models.Flag).filter(models.Flag.day_id == day_id).delete()
    for code in computed["flags"]:
        sess.add(
            models.Flag(
                day_id=day_id, code=code, detail=computed["breakdown"].get("note")
            )
        )
    sess.commit()

    # M6.1: дайджест дня сразу после закрытия — в очередь уведомлений.
    # Сбой доставки не влияет на закрытие: уведомление остается в очереди.
    try:
        from . import digest, notify  # локальный импорт: digest зависит от service

        d = digest.day_digest(sess, project, participant_id, date_iso)
        notify.on_day_closed(
            sess,
            project,
            participant_id,
            date_iso,
            text=digest.render_day_digest(d),
            payload=d,
        )
    except Exception:  # noqa: BLE001 — закрытие дня важнее дайджеста
        log.exception("не удалось поставить дайджест дня %s в очередь", day_id)

    return {**computed, "day_id": day_id, "sha256": sha, "closed": True}


def get_day(
    sess: Session,
    project: models.Project,
    participant_id: str,
    date_iso: str,
) -> dict:
    """Чтение дня: закрытый — из БД (неизменяемый), незакрытый — preview."""
    rec = find_day_record(sess, project, participant_id, date_iso)
    day_id = rec.day_id if rec is not None else day_id_for(project, participant_id, date_iso)
    if rec is not None and rec.closed:
        flags = [
            f.code
            for f in sess.scalars(
                select(models.Flag).where(models.Flag.day_id == day_id)
            )
        ]
        # M6.4: корректировки (append-only) и статус спора поверх закрытого дня
        adj_rows = sess.scalars(
            select(models.Adjustment).where(models.Adjustment.day_id == day_id)
        ).all()
        dispute_rows = sess.scalars(
            select(models.Dispute).where(models.Dispute.day_id == day_id)
        ).all()
        delta = sum(a.delta_cents for a in adj_rows)
        return {
            "participant_id": rec.participant_id,
            "date": rec.date,
            "tz": rec.tz,
            "paid_hours": str(Decimal(rec.paid_seconds) / calc.H),
            "paid_seconds": rec.paid_seconds,
            "amount_cents": rec.amount_cents,
            "amount_rub": str(Decimal(rec.amount_cents) / 100),
            "adjustments": [
                {"adjustment_id": a.adjustment_id, "delta_cents": a.delta_cents,
                 "reason": a.reason, "approved_by": a.approved_by}
                for a in adj_rows
            ],
            "adjustments_delta_cents": delta,
            "effective_amount_cents": rec.amount_cents + delta,
            "effective_amount_rub": str(Decimal(rec.amount_cents + delta) / 100),
            "disputes": [
                {"dispute_id": d.dispute_id, "status": d.status,
                 "interval_refs": d.interval_refs or []}
                for d in dispute_rows
            ],
            "share_bp": rec.share_bp,
            "flags": sorted(flags),
            "rate_version": rec.rate_version,
            "policy_version": rec.policy_version,
            "sha256": rec.sha256_input,
            "closed": True,
        }
    computed = compute_day(sess, project, participant_id, date_iso)
    computed["closed"] = False
    return computed
