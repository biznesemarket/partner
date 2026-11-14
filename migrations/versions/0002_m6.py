"""M6: споры дней и очередь уведомлений

Revision ID: 0002_m6
Revises: 0001_initial
Create Date: 2026-11-13

Metadata-driven, как 0001_initial, но создаются только новые таблицы M6
(disputes, notifications): таблицы 0001 уже созданы предыдущей миграцией
на живых установках. checkfirst=True делает миграцию идемпотентной для
свежих БД, где 0001 (тот же metadata) уже создал все таблицы.
"""
from __future__ import annotations

from alembic import op

from partner_server.db import Base

revision = "0002_m6"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

M6_TABLES = ("disputes", "notifications")


def upgrade() -> None:
    bind = op.get_bind()
    for name in M6_TABLES:
        table = Base.metadata.tables[name]
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(M6_TABLES):
        table = Base.metadata.tables[name]
        table.drop(bind=bind, checkfirst=True)
