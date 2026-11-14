"""M7: юридический модуль — payload_json в snapshots

Revision ID: 0003_m7
Revises: 0002_m6
Create Date: 2026-11-13

Снапшоты долей (M7.1) хранят полное содержимое (вклад и доли по участникам)
в новом колонке payload_json; sha256 подписываемого экспорта считается по
canonical JSON этой колонки.

На свежих БД 0001_initial создает snapshots уже по текущим ORM-моделям
(колонка payload_json есть), поэтому добавление колонки — с проверкой
наличия (идемпотентно, как checkfirst в 0002_m6).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_m7"
down_revision = "0002_m6"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    # offline-режим (--sql для PostgreSQL): inspect недоступен на
    # MockConnection — колонка добавляется безусловно (корректно для
    # живых установок, где 0001 старой версии ее еще не создала).
    try:
        insp = sa.inspect(bind)
        return any(c["name"] == column for c in insp.get_columns(table))
    except sa.exc.NoInspectionAvailable:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "snapshots", "payload_json"):
        op.add_column(
            "snapshots",
            sa.Column("payload_json", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "snapshots", "payload_json"):
        op.drop_column("snapshots", "payload_json")
