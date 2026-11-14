"""Подключение к БД: SQLAlchemy 2.0.

Один и тот же код моделей работает на PostgreSQL (prod, alembic-миграции)
и SQLite (local/demo — развитие режима embedded из прототипа M3).
Время — ISO 8601 UTC в TEXT; деньги — копейки (INTEGER); доля — базисные
пункты (TEXT с Decimal). Никаких float (calculator-spec.md §2).
"""
from __future__ import annotations

import pathlib
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(db_url: str | None = None):
    url = db_url or settings.db_url
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, future=True)


def make_sessionmaker(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


# Аудит-фикс F3 (P1): get_db() раньше вызывал make_engine() на КАЖДЫЙ запрос —
# у каждого запроса был собственный пул соединений, который после ответа
# доживал до GC. Под нагрузкой PostgreSQL это постоянное открытие/закрытие
# TCP-соединений и рост дескрипторов. Дефолтный engine создается один раз
# на URL и переиспользуется всеми запросами.
_default_engines: dict[str, object] = {}


def _default_engine(url: str):
    eng = _default_engines.get(url)
    if eng is None:
        eng = make_engine(url)
        _default_engines[url] = eng
    return eng


def get_db() -> Iterator[Session]:
    """FastAPI-зависимость. В тестах переопределяется через dependency_overrides."""
    engine = _default_engine(settings.db_url)
    s = make_sessionmaker(engine)()
    try:
        yield s
    finally:
        s.close()
