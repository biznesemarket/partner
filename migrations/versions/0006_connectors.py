"""v1.1: коннекторы git-платформ — события вебхуков и факты репозитория

Revision ID: 0006_connectors
Revises: 0005_m9
Create Date: 2026-11-14

Таблицы connector_events (идемпотентность доставок) и repo_facts
(ветки/коммиты/PR/issues/приемка для привязки сессий и no_output).
Metadata-driven и идемпотентно, как 0002_m6…0005_m9.
"""
from __future__ import annotations

from alembic import op

from partner_server.db import Base

revision = "0006_connectors"
down_revision = "0005_m9"
branch_labels = None
depends_on = None

CONNECTOR_TABLES = ("connector_events", "repo_facts")


def upgrade() -> None:
    bind = op.get_bind()
    for name in CONNECTOR_TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(CONNECTOR_TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
