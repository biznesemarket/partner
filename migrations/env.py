"""Alembic env: метаданные берутся из partner_server.models (единый источник схемы).

URL: PARTNER_DB_URL > sqlalchemy.url из alembic.ini. Offline-режим
(`alembic upgrade --sql`) используется в CI для проверки PostgreSQL DDL
без живого сервера БД.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# пакет partner_server доступен благодаря prepend_sys_path = apps
from partner_server.db import Base  # noqa: E402
from partner_server import models  # noqa: F401,E402

target_metadata = Base.metadata

db_url = os.getenv("PARTNER_DB_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
