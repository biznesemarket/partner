"""Точка входа сервера: uvicorn apps.partner_server.main:app.

Lifespan: при PARTNER_SCHEDULER=1 стартует поток закрытия дня 00:00
(ADR-0017); на старте выполняется догоняющий прогон незакрытых дней и
повтор недоставленных дайджестов (M6.3).
"""
from __future__ import annotations

import contextlib
import logging
import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, dayclose, service
from .config import settings
from .connector_routes import router as connector_router
from .db import make_engine, make_sessionmaker
from .routes import domain_error_status, router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    engine = make_engine()
    session_factory = make_sessionmaker(engine)
    # M9.1: состояние лицензии подписки — в лог при старте (grace/expired видны сразу)
    from . import licensing

    lic = licensing.current_state(settings)
    log = logging.getLogger("partner.license")
    log.info("лицензия: %s%s", lic.state, f" (tenant {lic.tenant_id}, до {lic.expires_at})" if lic.tenant_id else "")
    # SQLite (demo/embedded): схема создается автоматически.
    # PostgreSQL (prod): схема накатывается миграциями `alembic upgrade head`.
    if settings.db_url.startswith("sqlite"):
        from .db import Base

        Base.metadata.create_all(engine)
    # Догоняющий прогон: незакрытые дни закрываются при старте в порядке дат
    with session_factory() as sess:
        for project in sess.query(_projects_model()).all():
            today = _today_local(project.timezone)
            dayclose.close_catch_up(sess, project, today)
    if settings.scheduler_enabled:
        dayclose.start_scheduler_thread(session_factory)
    # M6.3: недоставленные дайджесты подхватываются при старте
    with session_factory() as sess:
        from . import notify

        result = notify.retry_pending(sess)
        if result["pending"]:
            logging.getLogger("partner.notify").info(
                "повтор доставки при старте: %s/%s", result["sent"], result["pending"]
            )
    yield
    # демонический поток планировщика останавливается вместе с процессом


def _projects_model():
    from . import models

    return models.Project


def _today_local(tz_name: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


app = FastAPI(
    title="Партнер — сервер учета времени",
    version=__version__,
    description=(
        "M4: прием heartbeat (идемпотентно), сборка и закрытие дня (00:00 MSK), "
        "объяснение начисления, месячные сводки. M5: антифрод-детекты "
        "(night/synthetic/replay/clock_skew/late), изоляция tenants, аудит. "
        "M6: дневной/месячный дайджесты, каналы доставки (agent_chat/email/webhook "
        "с повтором), спор дня с окном 3 дней и adjustment. "
        "M7: юридический модуль — снапшоты долей, гейты, экспорт юристу. "
        "M8: эксплуатационные метрики /v1/metrics, поставка Docker Compose. "
        "M9: лицензирование подписки, подпись устройств, docs-сайт, релиз v1.0. "
        "Правила расчета — golden-модуль partner_cli.calc (T-01..T-12)."
    ),
    lifespan=lifespan,
)
def _domain_error_body(exc: service.DomainError) -> dict:
    """Единый формат структурированной ошибки: как у HTTPException(detail={...})."""
    return {"detail": {"code": exc.code, "detail": exc.message}}


@app.exception_handler(service.DomainError)
async def _domain_error_handler(request: Request, exc: service.DomainError):
    """Fail closed: любой необработанный DomainError — 4xx, а не 500."""
    return JSONResponse(
        status_code=exc.status or domain_error_status(exc.code),
        content=_domain_error_body(exc),
    )


app.include_router(router)
app.include_router(connector_router)


@app.get("/")
def root():
    """Служебный ответ: имя сервиса, версия и ссылка на интерактивный OpenAPI."""
    return {"service": "partner-server", "version": app.version, "docs": "/docs"}


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    """Кабинет директора: статическая страница, данные — только через /v1 API."""
    return FileResponse(
        pathlib.Path(__file__).with_name("dashboard.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/healthz", include_in_schema=False)
def healthz_alias():
    """Корневой алиас /v1/healthz: healthcheck контейнера (deploy/) и
    внешний пинг документации (installation.md §2) живут на /healthz."""
    return {"status": "ok"}
