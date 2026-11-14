"""HTTP API M4–M6 (OpenAPI — docs/03-architecture/data-model.md §эндпоинты).

Аутентификация: Bearer-токен, если PARTNER_API_TOKEN задан; пустой токен —
demo-режим без авторизации (только local). Изоляция tenants — M5.
Дайджесты и споры дня — M6.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit
from . import digest
from . import disputes
from . import legal
from . import service
from .config import settings
from .db import get_db
from . import models

router = APIRouter(prefix="/v1")


# ---------- схемы запросов ----------


class SessionIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    participant_id: str
    project_id: str
    task_ref: str | None = None
    branch: str | None = None
    agent_attached: bool = True
    started_at_utc: str


class HeartbeatIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    session_id: str
    seq: int = Field(ge=0)
    cls: str = Field(pattern="^(active_human|supervised_agent|autonomous_agent|idle)$")
    start_utc: str
    end_utc: str
    signals: dict | None = None
    received_at_utc: str | None = None
    # Время отправки события устройством (ISO UTC). В MVP используется для
    # детектов clock_skew/late_rejected; подпись устройства — M8 (threat-model §4).


class PolicyIn(BaseModel):
    project_id: str
    params_json: dict
    approved_by: str
    reason: str = "обновление политики"


class RateVersionIn(BaseModel):
    policy_id: str
    effective_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    value_cents: int = Field(gt=0)
    reason: str
    approved_by: str


class DisputeIn(BaseModel):
    participant_id: str
    opened_by: str
    reason: str = Field(min_length=3)
    interval_ids: list[str] = Field(default_factory=list)


class DisputeResolveIn(BaseModel):
    decided_by: str
    decision: str = Field(pattern="^(approved|rejected)$")
    # required для approved (корректировка закрытого дня), иначе — ошибка домена
    delta_cents: int | None = None
    resolution: str | None = None


class GateIn(BaseModel):
    """Снятие гейта юристом: done требует evidence (документ/дата/подписант)."""

    status: str = Field(pattern="^(open|done)$")
    evidence: str = ""
    actor: str


class ActorIn(BaseModel):
    actor: str


# ---------- зависимости ----------


def require_token(authorization: str | None = Header(default=None)) -> None:
    from .config import settings

    # Сравнение в постоянном времени (hmac.compare_digest): защита от
    # timing-атак на Bearer-токен (threat-model §4, аудит-фикс F1/F10).
    expected = f"Bearer {settings.api_token}"
    if settings.api_token and not hmac.compare_digest(
        authorization or "", expected
    ):
        raise HTTPException(status_code=401, detail="требуется Bearer-токен")


def get_project(
    project_id: str | None = Query(default=None), db: Session = Depends(get_db)
) -> models.Project:
    if project_id:
        project = db.get(models.Project, project_id)
        if project is None:
            raise HTTPException(404, f"проект {project_id!r} не найден")
        return project
    project = db.scalars(select(models.Project).limit(1)).first()
    if project is None:
        raise HTTPException(404, "в БД нет проектов — выполните seed")
    return project


def get_participant_or_404(db: Session, participant_id: str) -> models.Participant:
    participant = db.get(models.Participant, participant_id)
    if participant is None:
        raise HTTPException(404, f"участник {participant_id!r} не найден")
    return participant


def check_tenant_isolation(
    db: Session, project: models.Project, participant_id: str
) -> models.Participant:
    """M5.3: deny by default. Кросс-тенантный запрос → 403 + инцидент в аудите."""
    participant = get_participant_or_404(db, participant_id)
    try:
        service.ensure_same_tenant(project, participant)
    except service.DomainError as err:
        audit.incident(
            db,
            code="cross_tenant_denied",
            entity="participants",
            entity_id=participant_id,
            meta={"project": project.project_id, "project_tenant": project.tenant_id,
                  "participant_tenant": participant.tenant_id},
        )
        raise HTTPException(403, {"code": err.code, "detail": err.message})
    return participant


def domain_error_status(code: str) -> int:
    if code in ("no_session", "no_policy"):
        return 404
    if code == "cross_tenant_denied":
        return 403
    return 422


def _status_for(err: service.DomainError) -> int:
    """Явный status ошибки домена (M9.1: license_* → 402) или по коду."""
    return err.status or domain_error_status(err.code)


@router.get("/healthz", tags=["system"])
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@router.get("/metrics", tags=["system"])
def metrics(db: Session = Depends(get_db)):
    """M8.4: эксплуатационные метрики в формате Prometheus text.

    Открыт без Bearer-токена (скрейп Prometheus), содержит только агрегаты —
    без персональных данных (runbook §алерты: алерты на days_open_overdue,
    digest_failed, day_close_delay).
    """
    from . import ops

    metrics = ops.collect(db)
    from . import licensing

    metrics.update(licensing.metrics_gauge(licensing.current_state(settings)))
    return Response(content=ops.render(metrics), media_type="text/plain")


@router.post("/sessions", status_code=201, tags=["tracking"])
def create_session(payload: SessionIn, db: Session = Depends(get_db), _: None = Depends(require_token)):
    existing = db.get(models.Session, payload.session_id)
    if existing is not None:
        return {"status": "duplicate", "session_id": payload.session_id}  # идемпотентно
    get_participant_or_404(db, payload.participant_id)
    project = db.get(models.Project, payload.project_id)
    if project is None:
        raise HTTPException(404, f"проект {payload.project_id!r} не найден")
    participant = get_participant_or_404(db, payload.participant_id)
    # M5.3: сессия не может соединять участников разных организаций
    try:
        service.ensure_same_tenant(project, participant)
    except service.DomainError as err:
        audit.incident(
            db,
            code="cross_tenant_denied",
            entity="sessions",
            entity_id=payload.session_id,
            meta={"project": payload.project_id,
                  "participant": payload.participant_id},
        )
        raise HTTPException(403, {"code": err.code, "detail": err.message})
    db.add(
        models.Session(
            session_id=payload.session_id,
            participant_id=payload.participant_id,
            project_id=payload.project_id,
            task_ref=payload.task_ref,
            branch=payload.branch,
            agent_attached=payload.agent_attached,
            started_at_utc=service.parse_utc(payload.started_at_utc).isoformat(timespec="seconds"),
        )
    )
    db.commit()
    return {"status": "created", "session_id": payload.session_id}


@router.post("/heartbeats", tags=["tracking"])
def post_heartbeat(
    payload: HeartbeatIn,
    db: Session = Depends(get_db),
    x_partner_device: str | None = Header(default=None),
    x_partner_sign: str | None = Header(default=None),
    _: None = Depends(require_token),
):
    # M9 (threat-model §4): подпись устройства HMAC-SHA256 по канонической
    # строке события. Заголовки необязательны при require_device_sign=0;
    # при 1 неподписанный батч отклоняется (unsigned_heartbeat).
    if x_partner_device or x_partner_sign or settings.require_device_sign:
        _verify_device_signature(db, payload, x_partner_device, x_partner_sign)
    try:
        result = service.ingest_heartbeat(
            db,
            event_id=payload.event_id,
            session_id=payload.session_id,
            seq=payload.seq,
            cls=payload.cls,
            start_utc=payload.start_utc,
            end_utc=payload.end_utc,
            received_at=(
                service.parse_utc(payload.received_at_utc)
                if payload.received_at_utc
                else datetime.now(timezone.utc)
            ),
            signals=payload.signals,
        )
    except service.DomainError as err:
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})
    return result


def _verify_device_signature(
    db: Session, payload: HeartbeatIn, device_id: str | None, sign: str | None
) -> None:
    import hmac as hmac_mod

    from partner_cli.signing import sign_heartbeat

    if not device_id or not sign:
        audit.incident(
            db, code="unsigned_heartbeat", entity="heartbeats",
            entity_id=payload.event_id, meta={"session": payload.session_id},
        )
        raise HTTPException(422, {"code": "device_signature_required", "detail": "подпись устройства обязательна (PARTNER_REQUIRE_DEVICE_SIGN=1)"})
    device = db.get(models.Device, device_id)
    if device is None or device.status != "active":
        audit.incident(
            db, code="device_invalid", entity="devices", entity_id=device_id,
            meta={"event": payload.event_id},
        )
        raise HTTPException(403, {"code": "device_invalid", "detail": "устройство не зарегистрировано или отозвано"})
    expected = sign_heartbeat(
        device.secret,
        payload.event_id,
        payload.session_id,
        payload.seq,
        payload.start_utc,
        payload.end_utc,
    )
    if not hmac_mod.compare_digest(sign, expected):
        audit.incident(
            db, code="device_signature_invalid", entity="devices",
            entity_id=device_id, meta={"event": payload.event_id},
        )
        raise HTTPException(403, {"code": "device_signature_invalid", "detail": "подпись heartbeat не сходится с секретом устройства"})


# ---------- устройства источников (threat-model §4, M9) ----------


class DeviceIn(BaseModel):
    participant_id: str
    label: str = Field(default="", max_length=128)


@router.post("/devices", status_code=201, tags=["devices"])
def register_device(payload: DeviceIn, db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Регистрация устройства агента: секрет выдается ОДИН раз (не читается повторно).

    Ротация: новое устройство + отзыв старого (threat-model §4).
    """
    get_participant_or_404(db, payload.participant_id)
    import secrets as pysecrets
    from datetime import datetime as dt

    device_id = f"dev-{pysecrets.token_hex(8)}"
    secret = pysecrets.token_hex(32)
    db.add(
        models.Device(
            device_id=device_id,
            participant_id=payload.participant_id,
            label=payload.label,
            secret=secret,
            status="active",
            created_at=dt.now(timezone.utc).isoformat(timespec="seconds"),
        )
    )
    audit.record(
        db, actor="director", entity="devices", entity_id=device_id,
        action="registered", new_value={"participant": payload.participant_id, "label": payload.label},
    )
    db.commit()
    return {"device_id": device_id, "secret": secret, "participant_id": payload.participant_id}


@router.post("/devices/{device_id}/revoke", tags=["devices"])
def revoke_device(device_id: str, db: Session = Depends(get_db), _: None = Depends(require_token)):
    device = db.get(models.Device, device_id)
    if device is None:
        raise HTTPException(404, f"устройство {device_id!r} не найдено")
    device.status = "revoked"
    audit.record(
        db, actor="director", entity="devices", entity_id=device_id,
        action="revoked", old_value={"status": "active"}, new_value={"status": "revoked"},
    )
    db.commit()
    return {"device_id": device_id, "status": "revoked"}


@router.get("/devices", tags=["devices"])
def list_devices(participant_id: str = Query(...), db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Список устройств участника — без секретов (они не хранятся в открытом виде для витрины)."""
    rows = db.scalars(
        select(models.Device).where(models.Device.participant_id == participant_id)
    ).all()
    return [
        {"device_id": d.device_id, "label": d.label, "status": d.status, "created_at": d.created_at}
        for d in rows
    ]


@router.get("/days/{date}", tags=["days"])
def get_day(date: str, participant_id: str, project: models.Project = Depends(get_project), db: Session = Depends(get_db), _: None = Depends(require_token)):
    # Аудит F1 (P0): суммы/доли дня — персональные финансовые данные;
    # читаются только с Bearer (data-model.md §аутентификация). Открыты
    # лишь задокументированно публичные /v1/metrics и /v1/license.
    check_tenant_isolation(db, project, participant_id)
    return service.get_day(db, project, participant_id, date)


@router.post("/days/{date}/close", tags=["days"])
def close_day(date: str, participant_id: str, project: models.Project = Depends(get_project), db: Session = Depends(get_db), _: None = Depends(require_token)):
    check_tenant_isolation(db, project, participant_id)
    try:
        return service.close_day(db, project, participant_id, date)
    except service.DomainError as err:
        raise HTTPException(_status_for(err), {"code": err.code, "detail": err.message})


@router.get("/license", tags=["system"])
def license_state():
    """M9.1: состояние лицензии подписки (ADR-0018) — без PII, открыто для
    мониторинга; используется дежурным и дашбордом установки."""
    from . import licensing

    return licensing.current_state(settings).as_dict()


@router.get("/days/{date}/explain", tags=["days"])
def explain_day(date: str, participant_id: str, project: models.Project = Depends(get_project), db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Объяснение начисления: интервалы, union-секунды по классам, капы, версии."""
    check_tenant_isolation(db, project, participant_id)
    pieces, flags, agent_attached = service.day_intervals(db, project, participant_id, date)
    computed = service.compute_day(db, project, participant_id, date)
    pol, _, params = service.load_policy(db, project.project_id)
    return {
        **computed,
        "explain": {
            "policy_id": pol.policy_id,
            "rate_version_applied": computed["rate_version"],
            "interval_union_rule": "R3: пересечение не суммируется",
            "antifraud_params": {
                "night_spike_hours": params.get("night_spike_hours", 6.0),
                "max_clock_skew_sec": params.get("max_clock_skew_sec", 120.0),
            },
            "raw_pieces": pieces and [
                {"start_s": p.interval.start_s, "end_s": p.interval.end_s,
                 "cls": p.interval.cls, "seconds": p.interval.seconds}
                for p in pieces
            ],
            "flags_source": flags,
            "agent_attached": agent_attached,
        },
    }


# Аудит F21 (P1): /status/summary регистрируется ДО /status/{participant_id} —
# иначе статический маршрут перебивается динамическим и всегда 404
# («участник 'summary' не найден»), эндпоинт недостижим.
@router.get("/status/summary", tags=["status"])
def status_summary(project: models.Project = Depends(get_project), db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Сводка директора: суммы по участникам за текущий месяц."""
    month = _current_month(project)
    rows = db.execute(
        select(
            models.DayRecord.participant_id,
            func.sum(models.DayRecord.paid_seconds),
            func.sum(models.DayRecord.amount_cents),
        )
        .where(models.DayRecord.date.like(f"{month}%"))
        .group_by(models.DayRecord.participant_id)
    ).all()
    return {
        "month": month,
        "participants": [
            {
                "participant_id": pid,
                "paid_hours_month": str(Decimal(ps or 0) / 3600),
                "amount_rub_month": str(Decimal(ac or 0) / 100),
            }
            for pid, ps, ac in rows
        ],
    }


@router.get("/status/{participant_id}", tags=["status"])
def status_month(
    participant_id: str,
    month: str | None = Query(default=None, pattern="^\\d{4}-\\d{2}$"),
    project: models.Project = Depends(get_project),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    check_tenant_isolation(db, project, participant_id)
    rows = db.execute(
        select(
            models.DayRecord.date,
            models.DayRecord.paid_seconds,
            models.DayRecord.amount_cents,
            models.DayRecord.share_bp,
        )
        .where(models.DayRecord.participant_id == participant_id)
        .where(models.DayRecord.date.like(f"{month or _current_month(project)}%"))
        .order_by(models.DayRecord.date)
    ).all()
    paid_seconds = sum(r.paid_seconds for r in rows)
    amount_cents = sum(r.amount_cents for r in rows)
    # Аудит-фикс F5 (P3): доли суммируются в Decimal (share_bp хранится
    # как TEXT-Decimal; float-суммирование нарушает calculator-spec.md §2).
    # Начальное значение Decimal(0): при пустом месяце quantize не падает.
    share_bp = sum((Decimal(r.share_bp or 0) for r in rows), Decimal(0))
    return {
        "participant_id": participant_id,
        "month": month or _current_month(project),
        "days": [
            {"date": r.date, "paid_seconds": r.paid_seconds, "amount_cents": r.amount_cents}
            for r in rows
        ],
        "paid_seconds_month": paid_seconds,
        "paid_hours_month": str(Decimal(paid_seconds) / 3600),
        "amount_cents_month": amount_cents,
        "amount_rub_month": str(Decimal(amount_cents) / 100),
        "share_bp_month": str(share_bp.quantize(Decimal("0.0001"))),
    }


def _current_month(project: models.Project | None = None) -> str:
    """Текущий месяц YYYY-MM. Аудит-фикс F12 (P3): по поясу проекта, а не UTC —
    закрытые дни датированы локальными датами проекта; у поясов с offset
    > +12 ч или < 0 месяц в границах суток расходится с UTC."""
    if project is not None:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(project.timezone)).isoformat()[:7]
    return datetime.now(timezone.utc).isoformat()[:7]


# ---------- дайджесты и споры (M6) ----------


@router.get("/digest/day/{date}", tags=["digest"])
def digest_day(
    date: str,
    participant_id: str,
    project: models.Project = Depends(get_project),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Дайджест закрытого дня: числа воспроизводимы из БД (DoD M6)."""
    check_tenant_isolation(db, project, participant_id)
    try:
        d = digest.day_digest(db, project, participant_id, date)
    except digest.DigestError as err:
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})
    return {**d, "text": digest.render_day_digest(d)}


@router.get("/digest/month/{month}", tags=["digest"])
def digest_month(
    month: str,
    participant_id: str,
    project: models.Project = Depends(get_project),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Месячный дайджест: часы, вклад, доли, остаток пула, pending."""
    check_tenant_isolation(db, project, participant_id)
    try:
        m = digest.month_digest(db, project, participant_id, month)
    except digest.DigestError as err:
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})
    return {**m, "text": digest.render_month_digest(m)}


@router.post("/days/{date}/disputes", status_code=201, tags=["disputes"])
def open_dispute(date: str, payload: DisputeIn, project: models.Project = Depends(get_project), db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Спор дня: разработчик помечает день (и опционально интервалы)."""
    check_tenant_isolation(db, project, payload.participant_id)
    try:
        return disputes.open_dispute(
            db,
            project,
            payload.participant_id,
            date,
            opened_by=payload.opened_by,
            reason=payload.reason,
            interval_ids=payload.interval_ids,
        )
    except disputes.DisputeError as err:
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})


@router.post("/disputes/{dispute_id}/resolve", tags=["disputes"])
def resolve_dispute(dispute_id: str, payload: DisputeResolveIn, db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Решение директора в окне 3 дней; approved → adjustment (append-only)."""
    try:
        return disputes.resolve_dispute(
            db,
            dispute_id,
            decided_by=payload.decided_by,
            decision=payload.decision,
            delta_cents=payload.delta_cents,
            resolution=payload.resolution,
        )
    except disputes.DisputeError as err:
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})


@router.get("/disputes", tags=["disputes"])
def list_disputes(
    status: str | None = Query(default=None, pattern="^(open|resolved|rejected|expired)$"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Список споров (директор): фильтр по статусу."""
    rows = disputes.list_disputes(db, status=status, limit=limit)
    return {
        "count": len(rows),
        "disputes": [
            {
                "dispute_id": d.dispute_id,
                "day_id": d.day_id,
                "participant_id": d.participant_id,
                "status": d.status,
                "opened_by": d.opened_by,
                "opened_at": d.opened_at,
                "window_deadline": d.window_deadline,
                "interval_refs": d.interval_refs or [],
                "reason": d.reason,
                "decided_by": d.decided_by,
                "decided_at": d.decided_at,
                "resolution": d.resolution,
            }
            for d in rows
        ],
    }


@router.get("/notifications", tags=["digest"])
def notifications_list(
    status: str | None = Query(default=None, pattern="^(pending|sent|failed)$"),
    recipient: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Журнал доставки дайджестов: канал, статус, attempts/last_error (M8.4)."""
    stmt = select(models.Notification).order_by(models.Notification.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(models.Notification.status == status)
    if recipient:
        stmt = stmt.where(models.Notification.recipient == recipient)
    rows = db.scalars(stmt).all()
    return {
        "count": len(rows),
        "notifications": [
            {
                "notification_id": n.notification_id,
                "kind": n.kind,
                "recipient": n.recipient,
                "channel": n.channel,
                "status": n.status,
                "attempts": n.attempts,
                "last_error": n.last_error,
                "created_at": n.created_at,
                "sent_at": n.sent_at,
                "text": n.text,
            }
            for n in rows
        ],
    }


# ---------- политика и аудит (M5.4) ----------


@router.post("/policies", status_code=201, tags=["policy"])
def create_policy_version(payload: PolicyIn, db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Новая версия политики: иммутабельна, действует с даты (R-4, threat-model §3)."""
    project = db.get(models.Project, payload.project_id)
    if project is None:
        raise HTTPException(404, f"проект {payload.project_id!r} не найден")
    current = db.scalars(
        select(models.Policy)
        .where(models.Policy.project_id == project.project_id)
        .order_by(models.Policy.policy_version.desc())
        .limit(1)
    ).first()
    old_params = dict(current.params_json) if current else None
    new_version = (current.policy_version + 1) if current else 1
    canonical = json.dumps(payload.params_json, sort_keys=True, ensure_ascii=False)
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    policy_id = f"pol-{project.project_id}-v{new_version}"
    db.add(
        models.Policy(
            policy_id=policy_id,
            project_id=project.project_id,
            policy_version=new_version,
            sha256=sha,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            params_json=payload.params_json,
        )
    )
    db.commit()
    audit.record(
        db,
        actor=payload.approved_by,
        entity="policies",
        entity_id=policy_id,
        action="policy.create",
        old_value=old_params,
        new_value=payload.params_json,
        meta={"reason": payload.reason, "version": new_version},
    )
    return {
        "status": "created",
        "policy_id": policy_id,
        "policy_version": new_version,
        "sha256": sha,
    }


@router.post("/rate-versions", status_code=201, tags=["policy"])
def create_rate_version(payload: RateVersionIn, db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Новая версия ставки. Заднее число запрещено (V-04, R-2)."""
    if db.get(models.Policy, payload.policy_id) is None:
        raise HTTPException(404, f"политика {payload.policy_id!r} не найдена")
    today = datetime.now(timezone.utc).date().isoformat()
    if payload.effective_from < today:
        audit.incident(
            db,
            code="rate_backdated",
            entity="rate_versions",
            entity_id=f"{payload.policy_id}|{payload.effective_from}",
            meta={"effective_from": payload.effective_from, "today": today},
        )
        raise HTTPException(
            422,
            {"code": "version_backdated",
             "detail": f"effective_from {payload.effective_from} раньше сегодня ({today}): заднее число запрещено (V-04)"},
        )
    existing = db.get(
        models.RateVersion, (payload.policy_id, payload.effective_from)
    )
    if existing is not None:
        return {"status": "duplicate", "policy_id": payload.policy_id,
                "effective_from": payload.effective_from}
    db.add(
        models.RateVersion(
            policy_id=payload.policy_id,
            effective_from=payload.effective_from,
            value_cents=payload.value_cents,
            reason=payload.reason,
            approved_by=payload.approved_by,
        )
    )
    db.commit()
    audit.record(
        db,
        actor=payload.approved_by,
        entity="rate_versions",
        entity_id=f"{payload.policy_id}|{payload.effective_from}",
        action="rate_version.create",
        new_value={"value_cents": payload.value_cents, "reason": payload.reason},
    )
    return {"status": "created", "policy_id": payload.policy_id,
            "effective_from": payload.effective_from}


@router.get("/audit", tags=["audit"])
def audit_list(
    entity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Журнал действий и инцидентов (директор). Append-only."""
    entries = audit.list_entries(db, entity=entity, limit=limit)
    return {
        "count": len(entries),
        "entries": [
            {
                "audit_id": e.audit_id,
                "ts": e.ts,
                "actor": e.actor,
                "entity": e.entity,
                "entity_id": e.entity_id,
                "action": e.action,
                "meta": e.meta,
            }
            for e in entries
        ],
    }


# ---------- юридический модуль (M7) ----------


def _legal_status(db: Session) -> dict:
    state = legal.load_state(pathlib.Path(settings.legal_file))
    return {
        "enabled": bool(state["legal"].get("enabled")),
        "enabled_at": state["legal"].get("enabled_at"),
        "unclosed_gates": legal.unclosed_gates(state),
        "gates": legal.gate_report(state),
    }


@router.get("/legal", tags=["legal"])
def legal_status(db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Статус юридического модуля и гейты G-01…G-08 (legal-module.md §4)."""
    return _legal_status(db)


@router.post("/legal/gates/{gate_id}", tags=["legal"])
def legal_set_gate(gate_id: str, payload: GateIn, db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Юрист снимает (done) или возвращает (open) гейт; done требует evidence."""
    try:
        entry = legal.set_gate(
            pathlib.Path(settings.legal_file),
            gate_id,
            status=payload.status,
            evidence=payload.evidence,
            actor=payload.actor,
        )
    except legal.LegalError as err:
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})
    audit.record(
        db,
        actor=payload.actor,
        entity="legal",
        entity_id=gate_id,
        action=f"gate.{payload.status}",
        new_value={"status": payload.status, "evidence": payload.evidence},
    )
    return entry


@router.post("/legal/enable", tags=["legal"])
def legal_enable(payload: ActorIn, db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Включение legal.enabled: true — только при снятых G-01…G-08 (M7.3).

    Незакрытые гейты → 422 legal_gates_not_cleared с перечнем.
    """
    before = legal.load_state(pathlib.Path(settings.legal_file))
    try:
        result = legal.enable(pathlib.Path(settings.legal_file))
    except legal.LegalError as err:
        audit.incident(
            db,
            code="legal_enable_blocked",
            entity="legal",
            entity_id="module",
            meta={"unclosed": legal.unclosed_gates(before), "actor": payload.actor},
        )
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})
    if result["status"] == "enabled":
        audit.record(
            db,
            actor=payload.actor,
            entity="legal",
            entity_id="module",
            action="legal.enable",
            old_value={"enabled": False},
            new_value={"enabled": True, "enabled_at": result["enabled_at"]},
        )
    return result


@router.post("/snapshots/{month}", tags=["legal"])
def snapshot_month(month: str, payload: ActorIn, project: models.Project = Depends(get_project), db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Месячный снапшот долей (M7.1): вклад ₽ и доли участников, sha256 + git-якорь.

    Иммутабелен: повтор с тем же содержимым — duplicate, с другим —
    snapshot_conflict (422).
    """
    try:
        result = legal.build_month_snapshot(db, project, month, actor=payload.actor)
    except legal.LegalError as err:
        raise HTTPException(domain_error_status(err.code), {"code": err.code, "detail": err.message})
    audit.record(
        db,
        actor=payload.actor,
        entity="snapshots",
        entity_id=result["snapshot_id"],
        action=f"snapshot.{result['status']}",
        new_value={"month": month, "sha256": result["sha256"], "git_ref": result.get("git_ref")},
    )
    return result


@router.get("/snapshots", tags=["legal"])
def snapshots_list(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Список месячных снапшотов (директор/юрист)."""
    rows = db.scalars(
        select(models.Snapshot).order_by(models.Snapshot.month.desc()).limit(limit)
    ).all()
    return {
        "count": len(rows),
        "snapshots": [
            {
                "snapshot_id": s.snapshot_id,
                "month": s.month,
                "sha256": s.sha256,
                "git_ref": s.git_ref,
                "created_at": s.created_at,
            }
            for s in rows
        ],
    }


@router.get("/snapshots/{snapshot_id}", tags=["legal"])
def snapshot_detail(snapshot_id: str, db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Снапшот с payload и подписываемым листом (render)."""
    s = db.get(models.Snapshot, snapshot_id)
    if s is None:
        raise HTTPException(404, f"снапшот {snapshot_id!r} не найден")
    return {
        "snapshot_id": s.snapshot_id,
        "month": s.month,
        "sha256": s.sha256,
        "git_ref": s.git_ref,
        "created_at": s.created_at,
        "payload": s.payload_json,
        "text": legal.snapshot_markdown(s.payload_json, s.sha256, s.git_ref),
    }


@router.post("/legal/export", tags=["legal"])
def legal_export(payload: ActorIn, project: models.Project = Depends(get_project), db: Session = Depends(get_db), _: None = Depends(require_token)):
    """Пакет юристу (M7.2): политика, снапшоты, гейты, вопросы, шаблоны M7.4.

    Без персональных данных; manifest.bundle_sha256 — подписываемый хеш.
    Файлы пакета раскладывает CLI: partner export-legal --to lawyer/.
    """
    bundle = legal.build_export_bundle(db, project, legal_file=pathlib.Path(settings.legal_file))
    audit.record(
        db,
        actor=payload.actor,
        entity="legal",
        entity_id="export",
        action="legal.export",
        new_value={"bundle_sha256": bundle["manifest"]["bundle_sha256"],
                   "snapshots": len(bundle["snapshots"])},
    )
    return bundle
