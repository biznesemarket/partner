"""Модели данных M4–M6 (data-model.md, ERD PostgreSQL; миграции 0001+0002).

Соответствие schema.sql прототипа сохранено; добавлены поля, потребные
серверу: policies.params_json (переменные мастера), sessions (task_ref,
agent_attached), споры (disputes) и очередь уведомлений (notifications) — M6.
Все PK — текстовые идентификаторы; день — participant|date.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))


class Project(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255))
    forge: Mapped[str] = mapped_column(String(32), default="github")
    repo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    default_branch: Mapped[str] = mapped_column(String(128), default="main")


class Participant(Base):
    __tablename__ = "participants"

    participant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(16),
        CheckConstraint("status IN ('active','paused','good_leaver','bad_leaver')"),
        default="active",
    )


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("forge", "forge_login", name="uq_identity"),)

    identity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.participant_id"), nullable=False
    )
    forge: Mapped[str] = mapped_column(String(32))
    forge_login: Mapped[str] = mapped_column(String(128))


class Policy(Base):
    __tablename__ = "policies"

    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), nullable=False
    )
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32))
    # Переменные политики (policy.schema.json) как JSON; на PostgreSQL — jsonb-совместимо
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)


class RateVersion(Base):
    """Версия ставки: заднее число запрещено (versioning.md V-02/V-04, проверка в коде)."""

    __tablename__ = "rate_versions"

    policy_id: Mapped[str] = mapped_column(
        ForeignKey("policies.policy_id"), primary_key=True
    )
    effective_from: Mapped[str] = mapped_column(String(10), primary_key=True)
    value_cents: Mapped[int] = mapped_column(
        Integer, CheckConstraint("value_cents > 0"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(128))


class ValuationVersion(Base):
    """Версия оценки проекта; обязательна ссылка на ADR (V-05)."""

    __tablename__ = "valuation_versions"

    policy_id: Mapped[str] = mapped_column(
        ForeignKey("policies.policy_id"), primary_key=True
    )
    effective_from: Mapped[str] = mapped_column(String(10), primary_key=True)
    value_rub: Mapped[int] = mapped_column(
        Integer, CheckConstraint("value_rub > 0"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(128))
    adr_ref: Mapped[str] = mapped_column(String(64))


class Session(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.participant_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), nullable=False
    )
    # NULL => no_task_link: интервалы учитываются, но не оплачиваются (time-accounting.md)
    task_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_attached: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at_utc: Mapped[str] = mapped_column(String(32))


class Heartbeat(Base):
    """event_id — ключ идемпотентности (защита от replay на уровне приема)."""

    __tablename__ = "heartbeats"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_session_seq"),)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[str] = mapped_column(String(32))
    signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Interval(Base):
    """Интервал активности, вытянутый из heartbeat/отчета агента. Append-only."""

    __tablename__ = "intervals"
    __table_args__ = (
        CheckConstraint(
            "cls IN ('active_human','supervised_agent','autonomous_agent','idle')",
            name="ck_interval_cls",
        ),
    )

    interval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id"), nullable=False
    )
    cls: Mapped[str] = mapped_column(String(24), nullable=False)
    start_utc: Mapped[str] = mapped_column(String(32), nullable=False)
    end_utc: Mapped[str] = mapped_column(String(32), nullable=False)


class DayRecord(Base):
    """Закрытый день. Append-only: правки — только adjustments."""

    __tablename__ = "day_records"

    day_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.participant_id"), nullable=False
    )
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # в поясе проекта
    tz: Mapped[str] = mapped_column(String(64), nullable=False)
    paid_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_cents: Mapped[int] = mapped_column(Integer, default=0)
    equity_cents: Mapped[int] = mapped_column(Integer, default=0)
    share_bp: Mapped[str] = mapped_column(String(32), nullable=False)  # Decimal → TEXT
    rate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    valuation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_input: Mapped[str] = mapped_column(String(64), nullable=False)
    closed: Mapped[int] = mapped_column(Integer, default=1)
    # M8.4: момент фактического закрытия (ISO UTC). Задержка закрытия дня —
    # closed_at минус 00:00 следующего дня в поясе tz (метрика /v1/metrics).
    closed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Adjustment(Base):
    """Корректировка закрытого дня (окно оспаривания 3 дня, dispute-flow M6)."""

    __tablename__ = "adjustments"

    adjustment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day_id: Mapped[str] = mapped_column(
        ForeignKey("day_records.day_id"), nullable=False
    )
    delta_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Flag(Base):
    """Антифрод/кап-флаги дня: cap_applied, autonomous_cap_applied, no_task_link…"""

    __tablename__ = "flags"

    day_id: Mapped[str] = mapped_column(
        ForeignKey("day_records.day_id"), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Snapshot(Base):
    """Месячный снапшот долей (M7): SHA-256 входных дневных записей + якорь в git.

    payload_json — сам снапшот (вклад, доли по участникам, версии политики);
    подписываемый экспорт юристу строится из payload_json + sha256. Снапшот
    иммутабелен: повтор за тот же месяц с другим содержимым — конфликт.
    """

    __tablename__ = "snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    git_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    # Содержимое снапшота (canonical JSON, M7.1); на PostgreSQL — jsonb-совместимо
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Dispute(Base):
    """Спор дня (M6.4): разработчик помечает интервалы, директор решает
    в окне 3 дней (time-accounting.md §спор). Результат approved —
    adjustment со ссылкой на day_record. Один открытый спор на день.
    """

    __tablename__ = "disputes"

    dispute_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day_id: Mapped[str] = mapped_column(
        ForeignKey("day_records.day_id"), nullable=False
    )
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.participant_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        CheckConstraint("status IN ('open','resolved','rejected','expired')"),
        default="open",
    )
    opened_by: Mapped[str] = mapped_column(String(128), nullable=False)
    opened_at: Mapped[str] = mapped_column(String(32), nullable=False)  # ISO UTC
    window_deadline: Mapped[str] = mapped_column(String(32), nullable=False)
    # Список interval_id, которые разработчик пометил как спорные
    interval_refs: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)


class Notification(Base):
    """Очередь доставки дайджестов/событий (M6.3).

    Каналы: agent_chat (основной), email (fallback), webhook (мессенджеры).
    status: pending → sent | failed; attempts + last_error для повтора.
    """

    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # day_digest | month_digest | dispute_opened | dispute_resolved
    recipient: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # канал, которым фактически доставлено
    status: Mapped[str] = mapped_column(
        String(16),
        CheckConstraint("status IN ('pending','sent','failed')"),
        default="pending",
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # отрендеренный текст дайджеста (детерминированный из чисел БД)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AuditLog(Base):
    """Аудит (M5.4): журнал действий и инцидентов — append-only.

    Фиксируется: кто (actor), когда (ts), что (entity/action), старое/новое
    значение (JSON), контекст инцидента (meta). Сюда же пишутся
    антифрод-инциденты: replay, duplicate_seq, cross_tenant_denied,
    unsigned_heartbeat.
    """

    __tablename__ = "audit_logs"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), nullable=False)  # ISO UTC
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(96), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ConnectorEvent(Base):
    """Принятый вебхук git-платформы (connectors.md §2–§5).

    delivery_id — ключ идемпотентности (X-GitHub-Delivery и аналоги);
    payload_json хранит только метаданные события, без кода и тел PR/issues.
    """

    __tablename__ = "connector_events"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    forge: Mapped[str] = mapped_column(String(32), nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), nullable=False
    )
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="webhook")


class RepoFact(Base):
    """Факт платформы: ветка, коммит, PR, issue, приемка (connectors.md §4).

    Факты — черновые метаданные для привязки сессий и антифрод-флага
    no_output; расчет денег по ним не выполняется.
    """

    __tablename__ = "repo_facts"
    __table_args__ = (
        Index("ix_repo_facts_project_kind", "project_id", "kind"),
        Index("ix_repo_facts_project_branch", "project_id", "branch"),
        Index("ix_repo_facts_project_task", "project_id", "task_ref"),
        Index("ix_repo_facts_occurred", "project_id", "occurred_at"),
    )

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), nullable=False
    )
    delivery_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    task_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Device(Base):
    """Устройство источника heartbeats (threat-model §4, реализация M9).

    Секрет устройства выдается один раз при регистрации (POST /v1/devices)
    и хранится только у агента; каждый heartbeat может подписываться
    HMAC-SHA256(secret, event_id|session_id|seq|start_utc|end_utc) в
    заголовках X-Partner-Device / X-Partner-Sign. PARTNER_REQUIRE_DEVICE_SIGN=1
    отбрасывает неподписанные батчи (threat-model: unsigned heartbeat).
    Отзыв — статус revoked (ротация: новое устройство + отзыв старого).
    """

    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.participant_id"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(128), default="")
    secret: Mapped[str] = mapped_column(String(64), nullable=False)  # hex, выдан один раз
    status: Mapped[str] = mapped_column(
        String(16),
        CheckConstraint("status IN ('active','revoked')"),
        default="active",
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)  # ISO UTC
