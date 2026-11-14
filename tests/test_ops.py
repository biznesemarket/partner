"""Тесты M8 (поставка и эксплуатация): метрики /v1/metrics, closed_at,
разовая джоба закрытия дня (dayclose_cli).

Сценарии:
- O-01  метрики: пустая БД — базовый набор метрик присутствует, формат Prometheus
- O-02  метрики: heartbeats_total растут после приема, received_at с offset (+03:00)
        считается корректно (регресс строкового сравнения)
- O-03  закрытие дня фиксирует closed_at; day_close_delay = закрытие − 00:00 след. дня
- O-04  повторное (идемпотентное) закрытие не перезаписывает closed_at
- O-05  days_open_overdue: интервал старше грейса без закрытого дня → 1;
        после закрытия дня → 0
- O-06  digest_pending/failed и disputes_open видны в метриках
- O-07  dayclose_cli: незакрытый день закрывается разовой джобой
- O-08  /v1/metrics доступен без Bearer-токена даже при заданном токене
        (скрейп Prometheus) и содержит только агрегаты
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


from partner_server import models, ops
from tests.conftest import BASE_DAY, moscow_local


def _hb(client, session_id, seq, cls, start, end):
    return client.post(
        "/v1/heartbeats",
        json={
            "event_id": f"e{seq}",
            "session_id": session_id,
            "seq": seq,
            "cls": cls,
            "start_utc": start,
            "end_utc": end,
            "received_at_utc": end,
        },
    )


# --- O-01 ----------------------------------------------------------------

def test_metrics_empty_db(client):
    r = client.get("/v1/metrics")
    assert r.status_code == 200
    body = r.text
    assert "partner_heartbeats_total 0" in body
    assert "partner_days_closed_total 0" in body
    assert "partner_days_open_overdue 0" in body
    assert "partner_digest_pending 0" in body
    assert "partner_disputes_open 0" in body
    # формат Prometheus: каждая строка — TYPE или значение
    for line in body.strip().splitlines():
        assert re.match(r"^(# TYPE \S+ (counter|gauge)|\S+ [\d.]+)$", line)


# --- O-02 ----------------------------------------------------------------

def test_metrics_count_heartbeats_with_tz_offset(client, factory):
    # received_at в MSK (+03:00) — должно считаться, а не сравниваться строкой
    now = datetime.now(timezone.utc)
    rec = (now - timedelta(minutes=10)).astimezone().isoformat(timespec="seconds")
    r = _hb(client, "s1", 0, "active_human", moscow_local(0), moscow_local(2))
    assert r.status_code == 200
    # перезапишем received_at на свежий с явным offset
    with factory() as s:
        hb = s.get(models.Heartbeat, "e0")
        hb.received_at = rec
        s.commit()
    body = client.get("/v1/metrics").text
    assert "partner_heartbeats_total 1" in body
    assert "partner_heartbeats_last_hour 1" in body


# --- O-03 ----------------------------------------------------------------

def test_close_day_records_closed_at_and_delay(client):
    _hb(client, "s1", 0, "active_human", moscow_local(0), moscow_local(6))
    r = client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    assert r.status_code == 200
    body = client.get("/v1/metrics").text
    m = re.search(r"partner_day_close_delay_seconds ([\d.]+)", body)
    assert m, "метрика задержки закрытия должна присутствовать"
    delay = float(m.group(1))
    # день 2026-11-12 закрыт сейчас (2026-11-13 UTC) → задержка порядка часов
    assert delay >= 0
    # полуночь 2026-11-13 MSK = 21:00 2026-11-12 UTC; закрытие — после этого
    midnight = BASE_DAY.replace(hour=21)  # 2026-11-12T21:00 UTC == 00:00 MSK 11-13
    assert delay >= (datetime.now(timezone.utc) - midnight).total_seconds() - 5


# --- O-04 ----------------------------------------------------------------

def test_reclose_keeps_first_closed_at(client, factory):
    _hb(client, "s1", 0, "active_human", moscow_local(0), moscow_local(6))
    client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    with factory() as s:
        first = s.get(models.DayRecord, "dev1|2026-11-12").closed_at
    client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    with factory() as s:
        assert s.get(models.DayRecord, "dev1|2026-11-12").closed_at == first


# --- O-05 ----------------------------------------------------------------

def test_days_open_overdue_counts_and_clears(client, factory):
    # интервал закончился двое суток назад, день не закрыт, грейс 24 ч → overdue
    _hb(
        client,
        "s1",
        0,
        "active_human",
        moscow_local(0, date_offset_days=-2),
        moscow_local(6, date_offset_days=-2),
    )
    body = client.get("/v1/metrics").text
    assert "partner_days_open_overdue 1" in body
    r = client.post(
        "/v1/days/2026-11-10/close",
        params={"participant_id": "dev1"},
    )
    assert r.status_code == 200
    assert "partner_days_open_overdue 0" in client.get("/v1/metrics").text


# --- O-06 ----------------------------------------------------------------

def test_digest_and_dispute_metrics(client, factory):
    _hb(client, "s1", 0, "active_human", moscow_local(0), moscow_local(6))
    client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    r = client.post(
        "/v1/days/2026-11-12/disputes",
        json={
            "participant_id": "dev1",
            "opened_by": "dev1",
            "reason": "ночной прогон не мой",
            "interval_ids": [],
        },
    )
    assert r.status_code == 201, r.text
    body = client.get("/v1/metrics").text
    assert "partner_disputes_open 1" in body
    # дайджест дня уходит в очередь agent_chat — доставка при close_day;
    # pending >= 1 или уже sent — проверяем, что метрики читаемы
    m = re.search(r"partner_digest_pending ([\d.]+)", body)
    assert m is not None


# --- O-07 ----------------------------------------------------------------

def test_dayclose_cli_closes_open_day(client, factory, tmp_path):
    _hb(client, "s1", 0, "active_human", moscow_local(0, date_offset_days=-1),
        moscow_local(6, date_offset_days=-1))
    from partner_server.dayclose_cli import main

    # та же SQLite-база, что в fixture factory (tmp_path общий для теста)
    rc = main([], db_url=f"sqlite:///{tmp_path/'test.db'}")
    assert rc == 0
    with factory() as s:
        rec = s.get(models.DayRecord, "dev1|2026-11-11")
        assert rec is not None and rec.closed == 1
        assert rec.amount_cents == 300_000  # 6 ч × 500 ₽, golden-числа
        assert rec.closed_at is not None
    # идемпотентность: повтор — без ошибок
    assert main([], db_url=f"sqlite:///{tmp_path/'test.db'}") == 0


# --- O-08 ----------------------------------------------------------------

def test_metrics_open_with_token_configured(client, factory):
    """Скрейп Prometheus без Bearer: /v1/metrics открыт, но не раскрывает дни."""
    from partner_server.config import settings

    prev = settings.api_token
    object.__setattr__(settings, "api_token", "sekret-token")
    try:
        assert client.post(
            "/v1/heartbeats",
            json={"event_id": "x", "session_id": "s1", "seq": 99,
                  "cls": "idle", "start_utc": moscow_local(0),
                  "end_utc": moscow_local(1)},
        ).status_code == 401
        r = client.get("/v1/metrics")
        assert r.status_code == 200
        assert "dev1" not in r.text and "fonii" not in r.text
    finally:
        object.__setattr__(settings, "api_token", prev)


# --- модуль ops: render стабилен и сортирован ----------------------------

def test_ops_render_sorted():
    out = ops.render({"partner_b_total": 2, "partner_a_gauge": 1.5})
    names = [line for line in out.splitlines() if not line.startswith("#")]
    assert names == ["partner_a_gauge 1.5", "partner_b_total 2"]
