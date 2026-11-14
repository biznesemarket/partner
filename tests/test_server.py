"""Интеграционные тесты сервера M4 (development-plan.md §M4.7).

Golden-parity (DoD M4): числа дней, полученные через HTTP API, обязаны
совпадать с golden-тестами T-01/T-02/T-04/T-05 из tests/test_calc.py.
БД — SQLite (режим embedded); один и тот же код моделей работает и на
PostgreSQL (DDL проверен offline-миграцией 0001_initial).

Фикстуры client/factory и helper moscow_local — в tests/conftest.py.
"""
from __future__ import annotations

import subprocess
import sys
import pathlib

from conftest import moscow_local  # helper из conftest (pytest добавляет tests/ в path)
from partner_server import models

DATE = "2026-11-12"
DATE_NEXT = "2026-11-13"


def hb(event: str, cls: str, start_h: int, end_h: int, seq: int, date_offset: int = 0) -> dict:
    """Heartbeat с интервалом по локальному времени MSK (H:00 → H:00).

    received_at_utc = конец интервала + 1 с — детерминированный прием
    (проверки clock_skew/late_rejected не срабатывают).
    """
    end_iso = moscow_local(end_h, date_offset_days=date_offset)
    return {
        "event_id": event,
        "session_id": "s1",
        "seq": seq,
        "cls": cls,
        "start_utc": moscow_local(start_h, date_offset_days=date_offset),
        "end_utc": moscow_local(end_h, date_offset_days=date_offset),
        "received_at_utc": end_iso,
    }


# ---------- golden parity: T-01 через API ----------


def test_t01_mixed_day_via_api(client):
    r = client.post(
        "/v1/heartbeats", json=hb("e1", "active_human", 10, 14, 0) | {"seq": 0}
    )
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    client.post("/v1/heartbeats", json=hb("e2", "supervised_agent", 14, 16, 1))
    client.post("/v1/heartbeats", json=hb("e3", "autonomous_agent", 16, 19, 2))
    client.post("/v1/heartbeats", json=hb("e4", "idle", 19, 20, 3))

    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    # T-01: 4+2+0.6 = 6,6 ч → 3 300 ₽ → 0,0330 %
    assert day["amount_cents"] == 330_000
    assert day["paid_hours"] == "6.6"
    assert day["share_bp"] == "3.3000"
    assert day["closed"] is False  # preview: закрытие — только через close

    closed = client.post(
        "/v1/days/2026-11-12/close", params={"participant_id": "dev1"}
    ).json()
    assert closed["closed"] is True
    assert closed["amount_cents"] == 330_000
    assert closed["sha256"]


def test_t02_night_autonomous_via_api(client):
    """T-02: ночь 8 ч автономной работы (00:00–08:00 MSK) → 800 ₽, не 4 000 ₽."""
    client.post("/v1/heartbeats", json=hb("n1", "autonomous_agent", 0, 8, 0))
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 80_000  # 1,6 оплач. ч × 500 ₽
    assert day["paid_hours"] == "1.6"
    assert "autonomous_cap_applied" not in day["flags"]


def test_t04_parallel_union_via_api(client):
    """T-04: две перекрывающиеся сессии 10–12 и 11–13 → union 3 ч, не 4 ч."""
    client.post(
        "/v1/sessions",
        json={
            "session_id": "s2", "participant_id": "dev1", "project_id": "partner",
            "task_ref": "T-2", "started_at_utc": moscow_local(0),
        },
    )
    client.post("/v1/heartbeats", json=hb("p1", "active_human", 10, 12, 0))
    client.post(
        "/v1/heartbeats",
        json={
            "event_id": "p2", "session_id": "s2", "seq": 0,
            "cls": "active_human",
            "start_utc": moscow_local(11), "end_utc": moscow_local(13),
            "received_at_utc": moscow_local(13),
        },
    )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 150_000
    assert day["paid_hours"] == "3"


def test_t05_midnight_split_via_api(client):
    """T-05: интервал 22:00–02:00 (через полночь) → 1 000 ₽ дню D и 1 000 ₽ дню D+1."""
    client.post(
        "/v1/heartbeats",
        json={
            "event_id": "m1", "session_id": "s1", "seq": 0,
            "cls": "active_human",
            "start_utc": moscow_local(22),
            "end_utc": moscow_local(2, date_offset_days=1),
            "received_at_utc": moscow_local(2, date_offset_days=1),
        },
    )
    d = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    n = client.get("/v1/days/2026-11-13", params={"participant_id": "dev1"}).json()
    assert d["amount_cents"] == 100_000
    assert n["amount_cents"] == 100_000


# ---------- идемпотентность и защита ----------


def test_heartbeat_replay_is_idempotent(client):
    """Повтор того же event_id не дублирует интервал и не меняет сумму дня."""
    client.post("/v1/heartbeats", json=hb("r1", "active_human", 10, 14, 0))
    r = client.post("/v1/heartbeats", json=hb("r1", "active_human", 10, 14, 0))
    assert r.json() == {"status": "duplicate", "event_id": "r1"}

    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 200_000  # 4 ч × 500 ₽, один интервал

    ex = client.get(
        "/v1/days/2026-11-12/explain", params={"participant_id": "dev1"}
    ).json()
    assert len(ex["explain"]["raw_pieces"]) == 1


def test_duplicate_seq_rejected(client):
    client.post("/v1/heartbeats", json=hb("q1", "active_human", 10, 12, 0))
    r = client.post(
        "/v1/heartbeats",
        json={
            "event_id": "q2", "session_id": "s1", "seq": 0,  # тот же seq, другой event
            "cls": "active_human",
            "start_utc": moscow_local(12), "end_utc": moscow_local(13),
            "received_at_utc": moscow_local(13),
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_seq"


def test_heartbeat_without_session_404(client):
    r = client.post(
        "/v1/heartbeats",
        json={
            "event_id": "x1", "session_id": "ghost", "seq": 0,
            "cls": "active_human",
            "start_utc": moscow_local(10), "end_utc": moscow_local(11),
        },
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "no_session"


def test_bad_interval_rejected(client):
    r = client.post(
        "/v1/heartbeats",
        json={
            "event_id": "b1", "session_id": "s1", "seq": 9,
            "cls": "active_human",
            "start_utc": moscow_local(12), "end_utc": moscow_local(10),
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "bad_interval"


def test_close_is_idempotent_same_sha(client):
    """Повторное закрытие дня дает тот же sha256 (политика неизменна)."""
    client.post("/v1/heartbeats", json=hb("c1", "active_human", 10, 14, 0))
    a = client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"}).json()
    b = client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"}).json()
    assert a["sha256"] == b["sha256"]


def test_no_task_link_not_paid(client):
    """Сессия без task_ref: время учитывается, но не оплачивается (no_task_link)."""
    client.post(
        "/v1/sessions",
        json={
            "session_id": "s3", "participant_id": "dev1", "project_id": "partner",
            "task_ref": None, "started_at_utc": moscow_local(0),
        },
    )
    client.post(
        "/v1/heartbeats",
        json={
            "event_id": "t1", "session_id": "s3", "seq": 0,
            "cls": "active_human",
            "start_utc": moscow_local(9), "end_utc": moscow_local(13),
            "received_at_utc": moscow_local(13),
        },
    )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 0
    assert "no_task_link" in day["flags"]


def test_manual_bill_policy_is_rejected_with_422(client, factory):
    """T-05: manual_only_policy=bill без агента — 422, а не 500."""
    with factory() as s:
        policy = s.get(models.Policy, "p1")
        policy.params_json = {**policy.params_json, "manual_only_policy": "bill"}
        s.commit()

    client.post(
        "/v1/sessions",
        json={
            "session_id": "s-manual", "participant_id": "dev1", "project_id": "partner",
            "task_ref": "T-1", "agent_attached": False,
            "started_at_utc": moscow_local(0),
        },
    )
    client.post(
        "/v1/heartbeats",
        json={
            "event_id": "mb1", "session_id": "s-manual", "seq": 0,
            "cls": "active_human",
            "start_utc": moscow_local(10), "end_utc": moscow_local(14),
            "received_at_utc": moscow_local(14),
        },
    )

    for method, path in (
        ("get", "/v1/days/2026-11-12"),
        ("get", "/v1/days/2026-11-12/explain"),
        ("post", "/v1/days/2026-11-12/close"),
    ):
        response = getattr(client, method)(path, params={"participant_id": "dev1"})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "manual_only_policy_not_supported"


def test_domain_error_handler_uses_structured_envelope():
    """Глобальный обработчик DomainError отдает тот же envelope, что и routes."""
    from partner_server import service
    from partner_server.main import _domain_error_body

    body = _domain_error_body(
        service.DomainError("manual_only_policy_not_supported", "x")
    )
    assert body == {
        "detail": {"code": "manual_only_policy_not_supported", "detail": "x"}
    }


def test_status_month_aggregates(client):
    client.post("/v1/heartbeats", json=hb("a1", "active_human", 10, 14, 0))
    client.post(
        "/v1/heartbeats",
        json={
            "event_id": "a2", "session_id": "s1", "seq": 1,
            "cls": "active_human",
            "start_utc": moscow_local(10, date_offset_days=1),
            "end_utc": moscow_local(12, date_offset_days=1),
            "received_at_utc": moscow_local(12, date_offset_days=1),
        },
    )
    client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    client.post("/v1/days/2026-11-13/close", params={"participant_id": "dev1"})
    st = client.get(
        "/v1/status/dev1", params={"month": "2026-11"}
    ).json()
    # 4 ч (2 000 ₽) + 2 ч (1 000 ₽) = 3 000 ₽
    assert st["amount_cents_month"] == 300_000
    assert st["paid_hours_month"] == "6"


def test_auth_required_when_token_set(client):
    """PARTNER_API_TOKEN включает проверку Bearer (prod-режим)."""
    from partner_server.config import settings as live_settings

    object.__setattr__(live_settings, "api_token", "secret-token")
    try:
        assert client.post("/v1/sessions", json={}).status_code == 401
        client.headers.update({"Authorization": "Bearer wrong"})
        assert client.post("/v1/sessions", json={}).status_code == 401
        client.headers.update({"Authorization": "Bearer secret-token"})
        assert client.post(
            "/v1/sessions",
            json={
                "session_id": "s-auth", "participant_id": "dev1",
                "project_id": "partner", "started_at_utc": moscow_local(0),
            },
        ).status_code == 201
    finally:
        object.__setattr__(live_settings, "api_token", "")
        client.headers.clear()


def test_alembic_matches_models(tmp_path):
    """Миграция 0001_initial создает схему, совпадающую с ORM-моделями."""
    import os

    repo = pathlib.Path(__file__).resolve().parent.parent
    db_path = tmp_path / "mig.db"
    # PATH и системные переменные наследуются от текущего процесса: подмена
    # PATH на POSIX-пути ломала запуск Python на Windows (WinError 10106).
    env = {
        **os.environ,
        "PARTNER_DB_URL": f"sqlite:///{db_path.as_posix()}",
        "PYTHONIOENCODING": "utf-8",
    }
    r1 = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    assert r1.returncode == 0, r1.stderr
    r2 = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=repo, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    assert r2.returncode == 0, r2.stdout + r2.stderr
