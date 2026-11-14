"""M9: релиз v1.0 — устройства источников (подпись heartbeat, threat-model §4)

Revision ID: 0005_m9
Revises: 0004_m8
Create Date: 2026-11-13

Таблица devices: device_id, participant_id, label, secret (hex, выдается
один раз при регистрации), status active/revoked, created_at.
Подпись heartbeat: X-Partner-Sign = HMAC-SHA256(secret, payload-line);
PARTNER_REQUIRE_DEVICE_SIGN=1 отбрасывает неподписанные батчи.

На свежих БД 0001_initial создает devices по ORM-моделям; здесь — создание
с проверкой наличия (идемпотентно, как 0002_m6…0004_m8).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_m9"
down_revision = "0004_m8"
branch_labels = None
depends_on = None

_TABLE_COLUMNS = [
    sa.Column("device_id", sa.String(length=64), primary_key=True),
    sa.Column("participant_id", sa.String(length=64), sa.ForeignKey("participants.participant_id"), nullable=False),
    sa.Column("label", sa.String(length=128)),
    sa.Column("secret", sa.String(length=64), nullable=False),
    sa.Column("status", sa.String(length=16)),
    sa.Column("created_at", sa.String(length=32), nullable=False),
]


def _has_table(bind, table: str) -> bool:
    try:
        insp = sa.inspect(bind)
        return insp.has_table(table)
    except sa.exc.NoInspectionAvailable:
        # offline --sql для PG: таблица создается безусловно (для живых
        # установок 0001 старой версии ее не создала)
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "devices"):
        sa.Table("devices", sa.MetaData(), *_TABLE_COLUMNS).create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "devices"):
        op.drop_table("devices")
