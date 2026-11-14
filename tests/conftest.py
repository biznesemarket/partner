"""Общие фикстуры серверных тестов (M4/M5): SQLite-БД, seed tenant #1, TestClient."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from partner_server import models
from partner_server.db import Base, make_engine, make_sessionmaker
from partner_server.routes import get_db
from partner_server.main import app

TZ = "Europe/Moscow"  # UTC+3, переводов часов нет
BASE_DAY = datetime(2026, 11, 12, tzinfo=timezone.utc)


def moscow_local(hour: int, minute: int = 0, date_offset_days: int = 0) -> str:
    """Локальное время MSK на фиксированную дату 2026-11-12 → ISO UTC.

    hour >= 24 означает следующий день: 26 ч = 02:00 следующего дня.
    """
    day = BASE_DAY + timedelta(days=date_offset_days)
    day = day + timedelta(hours=hour)  # час может «зайти» на следующие сутки
    hour = hour % 24
    # Moscow = UTC+3: локальный H:00 соответствует (H-3):00 UTC
    return (
        datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc
        )
        - timedelta(hours=3)
    ).isoformat(timespec="seconds")


@pytest.fixture()
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path/'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = make_sessionmaker(engine)

    with session_factory() as s:
        s.add(models.Tenant(tenant_id="fonii", name="ООО ФОНИИ"))
        s.add(
            models.Project(
                project_id="partner", tenant_id="fonii", name="Партнер",
                timezone=TZ,
            )
        )
        s.add(models.Participant(participant_id="dev1", tenant_id="fonii", display_name="Разработчик"))
        s.add(
            models.Session(
                session_id="s1", participant_id="dev1", project_id="partner",
                task_ref="T-1", branch="dev/dev1/main",
                started_at_utc=moscow_local(0),
            )
        )
        s.add(
            models.Policy(
                policy_id="p1", project_id="partner", policy_version=1,
                sha256="0" * 64, created_at="2026-11-01T00:00:00+00:00",
                params_json={
                    "rate_rub_per_hour": 500,
                    "valuation_rub": 10_000_000,
                    "daily_cap_hours": 12.0,
                    "autonomous_paid_cap_hours": 2.0,
                    "k_supervised": 1.0,
                    "k_autonomous": 0.2,
                },
            )
        )
        s.add(
            models.RateVersion(
                policy_id="p1", effective_from="2026-10-01",
                value_cents=50_000, reason="старт", approved_by="директор",
            )
        )
        s.commit()
    return session_factory


@pytest.fixture()
def client(factory):
    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:  # lifespan: catch-up
        yield test_client
    app.dependency_overrides.pop(get_db, None)
