"""Аудит (M5.4, threat-model.md §«изоляция и периметр»): append-only журнал.

Требование development-plan.md §5.4: кто, когда, старое/новое значение.
Инциденты безопасности (replay, duplicate_seq, cross_tenant_denied,
unsigned_heartbeat) пишутся тем же механизмом с action "incident.*".
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


def record(
    sess: Session,
    *,
    actor: str,
    entity: str,
    entity_id: str,
    action: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
    meta: dict | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """Запись в журнал. Возвращает audit_id."""
    audit_id = uuid.uuid4().hex
    sess.add(
        models.AuditLog(
            audit_id=audit_id,
            tenant_id=tenant_id,
            project_id=project_id,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor=actor,
            entity=entity,
            entity_id=entity_id,
            action=action,
            old_value=old_value,
            new_value=new_value,
            meta=meta,
        )
    )
    sess.commit()
    return audit_id


def incident(
    sess: Session,
    *,
    code: str,
    entity: str,
    entity_id: str,
    meta: dict | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """Инцидент безопасности: actor = system-antifraud."""
    return record(
        sess,
        actor="system-antifraud",
        entity=entity,
        entity_id=entity_id,
        action=f"incident.{code}",
        meta=meta,
        tenant_id=tenant_id,
        project_id=project_id,
    )


def list_entries(
    sess: Session,
    entity: str | None = None,
    limit: int = 100,
    tenant_id: str | None = None,
    project_id: str | None = None,
):
    stmt = select(models.AuditLog).order_by(models.AuditLog.ts.desc()).limit(limit)
    if entity:
        stmt = stmt.where(models.AuditLog.entity == entity)
    if tenant_id:
        stmt = stmt.where(models.AuditLog.tenant_id == tenant_id)
    if project_id:
        stmt = stmt.where(models.AuditLog.project_id == project_id)
    return sess.scalars(stmt).all()
