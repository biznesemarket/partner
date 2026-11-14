"""v1.2 tenant/RBAC: project_id в day_records, область tenant/project в
notifications и audit_logs (issue #4).

Revision ID: 0007_tenant_rbac
Revises: 0006_connectors
Create Date: 2026-11-14

День теперь принадлежит проекту: DayRecord.project_id обязателен, а
project-scoped day_id формируется сервисом для новых записей. Исторические
записи ``participant|date`` сохраняются и читаются через find_day_record;
project_id для них восстанавливается по сессиям участника.

Уведомления и аудит получают tenant_id/project_id для фильтрации списков.
Строки без восстановимой области остаются NULL и доступны только глобальному
административному токену.
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0007_tenant_rbac"
down_revision = "0006_connectors"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    try:
        return {c["name"] for c in sa.inspect(bind).get_columns(table)}
    except sa.exc.NoInspectionAvailable:
        return set()


def _indexes(bind, table: str) -> set[str]:
    try:
        return {i["name"] for i in sa.inspect(bind).get_indexes(table)}
    except sa.exc.NoInspectionAvailable:
        return set()


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _scope_by_project(bind, project_id: str | None) -> tuple[str | None, str | None]:
    if not project_id:
        return None, None
    tenant_id = bind.execute(
        sa.text("SELECT tenant_id FROM projects WHERE project_id = :p"),
        {"p": project_id},
    ).scalar()
    return tenant_id, project_id


def _audit_scope(bind, entity: str, entity_id: str, meta: dict) -> tuple[str | None, str | None]:
    """Восстановить область исторической записи аудита по сущности."""
    project_id = meta.get("project") or meta.get("project_id")
    policy_id = None
    if entity == "policies":
        policy_id = entity_id
    elif entity == "rate_versions":
        policy_id = entity_id.split("|", 1)[0]
    if policy_id:
        row = bind.execute(
            sa.text("SELECT project_id FROM policies WHERE policy_id = :p"),
            {"p": policy_id},
        ).first()
        project_id = project_id or (row[0] if row else None)
    if not project_id and entity == "sessions":
        row = bind.execute(
            sa.text("SELECT project_id FROM sessions WHERE session_id = :s"),
            {"s": entity_id},
        ).first()
        project_id = row[0] if row else None
    if not project_id and entity == "disputes":
        row = bind.execute(
            sa.text("SELECT project_id FROM disputes WHERE dispute_id = :d"),
            {"d": entity_id},
        ).first()
        project_id = row[0] if row else None
    if not project_id and entity == "snapshots":
        row = bind.execute(
            sa.text("SELECT payload_json FROM snapshots WHERE snapshot_id = :s"),
            {"s": entity_id},
        ).first()
        project_id = _json(row[0]).get("project_id") if row else None
    if project_id:
        return _scope_by_project(bind, project_id)

    tenant_id = None
    if entity == "participants":
        tenant_id = bind.execute(
            sa.text("SELECT tenant_id FROM participants WHERE participant_id = :p"),
            {"p": entity_id},
        ).scalar()
    elif entity == "devices":
        tenant_id = bind.execute(
            sa.text(
                "SELECT pt.tenant_id FROM devices d "
                "JOIN participants pt ON pt.participant_id = d.participant_id "
                "WHERE d.device_id = :d"
            ),
            {"d": entity_id},
        ).scalar()
    return tenant_id, None


def upgrade() -> None:
    bind = op.get_bind()

    day_cols = _columns(bind, "day_records")
    day_added = "project_id" not in day_cols
    if day_added:
        op.add_column(
            "day_records", sa.Column("project_id", sa.String(length=64), nullable=True)
        )
    # Восстановление проекта исторических дней: сначала по сессиям участника,
    # затем по первому проекту его tenant.
    op.execute(
        """
        UPDATE day_records SET project_id = (
            SELECT s.project_id FROM sessions s
            WHERE s.participant_id = day_records.participant_id
            ORDER BY s.started_at_utc DESC LIMIT 1
        ) WHERE project_id IS NULL OR project_id = ''
        """
    )
    op.execute(
        """
        UPDATE day_records SET project_id = (
            SELECT p.project_id FROM projects p
            JOIN participants pt ON pt.tenant_id = p.tenant_id
            WHERE pt.participant_id = day_records.participant_id
            ORDER BY p.project_id LIMIT 1
        ) WHERE project_id IS NULL OR project_id = ''
        """
    )
    # Последний резерв для одиночной установки: единственный проект системы.
    op.execute(
        """
        UPDATE day_records SET project_id = (
            SELECT p.project_id FROM projects p ORDER BY p.project_id LIMIT 1
        ) WHERE project_id IS NULL OR project_id = ''
        """
    )
    with op.batch_alter_table("day_records") as batch:
        batch.alter_column(
            "project_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    if "ix_day_records_project_date" not in _indexes(bind, "day_records"):
        op.create_index(
            "ix_day_records_project_date",
            "day_records",
            ["project_id", "date"],
            unique=False,
        )

    for table in ("notifications", "audit_logs"):
        cols = _columns(bind, table)
        if "tenant_id" not in cols:
            op.add_column(table, sa.Column("tenant_id", sa.String(length=64), nullable=True))
        if "project_id" not in cols:
            op.add_column(table, sa.Column("project_id", sa.String(length=64), nullable=True))

    # Уведомления: project_id/tenant_id из payload (day_digest, month_digest,
    # dispute_*), для legacy day_id — через day_records.
    rows = bind.execute(
        sa.text(
            "SELECT notification_id, payload_json FROM notifications "
            "WHERE tenant_id IS NULL OR project_id IS NULL"
        )
    ).fetchall()
    for notification_id, payload_raw in rows:
        payload = _json(payload_raw)
        project_id = payload.get("project_id")
        if not project_id and payload.get("day_id"):
            rec = bind.execute(
                sa.text("SELECT project_id FROM day_records WHERE day_id = :d"),
                {"d": payload["day_id"]},
            ).first()
            project_id = rec[0] if rec else None
        tenant_id, project_id = _scope_by_project(bind, project_id)
        if tenant_id or project_id:
            bind.execute(
                sa.text(
                    "UPDATE notifications SET tenant_id = :t, project_id = :p "
                    "WHERE notification_id = :n"
                ),
                {"t": tenant_id, "p": project_id, "n": notification_id},
            )

    # Аудит: область из meta/сущности; невосстановимые остаются NULL.
    rows = bind.execute(
        sa.text(
            "SELECT audit_id, entity, entity_id, meta FROM audit_logs "
            "WHERE tenant_id IS NULL"
        )
    ).fetchall()
    for audit_id, entity, entity_id, meta_raw in rows:
        tenant_id, project_id = _audit_scope(
            bind, entity or "", entity_id or "", _json(meta_raw)
        )
        if tenant_id or project_id:
            bind.execute(
                sa.text(
                    "UPDATE audit_logs SET tenant_id = :t, project_id = :p "
                    "WHERE audit_id = :a"
                ),
                {"t": tenant_id, "p": project_id, "a": audit_id},
            )

    if "ix_notifications_tenant_project" not in _indexes(bind, "notifications"):
        op.create_index(
            "ix_notifications_tenant_project",
            "notifications",
            ["tenant_id", "project_id"],
            unique=False,
        )
    if "ix_audit_logs_tenant_project" not in _indexes(bind, "audit_logs"):
        op.create_index(
            "ix_audit_logs_tenant_project",
            "audit_logs",
            ["tenant_id", "project_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_tenant_project", table_name="audit_logs")
    op.drop_index("ix_notifications_tenant_project", table_name="notifications")
    op.drop_column("audit_logs", "project_id")
    op.drop_column("audit_logs", "tenant_id")
    op.drop_column("notifications", "project_id")
    op.drop_column("notifications", "tenant_id")
    op.drop_index("ix_day_records_project_date", table_name="day_records")
    with op.batch_alter_table("day_records") as batch:
        batch.drop_column("project_id")
