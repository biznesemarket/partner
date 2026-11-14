"""M8: поставка и эксплуатация — closed_at в day_records

Revision ID: 0004_m8
Revises: 0003_m7
Create Date: 2026-11-13

Метрика закрытия дня (M8.4): колонка closed_at хранит момент фактического
закрытия (ISO UTC); задержка = closed_at - 00:00 следующего дня в поясе
записи. Для уже закрытых дней до M8 колонка NULL — метрика по ним
не считается (что и требовалось: исторические дни закрывались вручную).

На свежих БД 0001_initial создает day_records по текущим ORM-моделям
(колонка уже есть), поэтому добавление — с проверкой наличия
(идемпотентно, как в 0002_m6/0003_m7).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_m8"
down_revision = "0003_m7"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    # offline-режим (--sql для PostgreSQL): колонка добавляется безусловно —
    # корректно для живых установок, где 0001 старой версии ее еще не создала.
    try:
        insp = sa.inspect(bind)
        return any(c["name"] == column for c in insp.get_columns(table))
    except sa.exc.NoInspectionAvailable:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "day_records", "closed_at"):
        op.add_column(
            "day_records",
            sa.Column("closed_at", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "day_records", "closed_at"):
        op.drop_column("day_records", "closed_at")
