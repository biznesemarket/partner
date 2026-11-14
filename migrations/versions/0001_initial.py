"""initial schema M4/M5: 15 tables (incl. audit_logs)

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-13

Миграция metadata-driven: таблицы создаются из partner_server.models
(единый источник схемы вместе с ORM). Изменение модели без миграции
поймается тестом соответствия (tests/test_server.py::test_alembic_matches_models).
"""
from __future__ import annotations

from alembic import op

from partner_server.db import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in Base.metadata.sorted_tables:
        table.create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(Base.metadata.sorted_tables):
        # checkfirst: к моменту отката 0001 таблицы M6/M7 могут быть
        # уже удалены их миграциями (0002_m6, 0003_m7)
        table.drop(bind=bind, checkfirst=True)
