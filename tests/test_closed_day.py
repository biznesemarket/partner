"""v1.2 ADR-0021: инвариант закрытого дня.

Принятый day_record не перезаписывается: повторный пересчет с теми же
данными идемпотентен, поздние события создают append-only adjustment,
повторная компенсация не дублируется.
"""
from __future__ import annotations

from conftest import moscow_local

from partner_server import models

DATE = "2026-11-12"


def hb(event: str, start_h: int, end_h: int, seq: int) -> dict:
    return {
        "event_id": event,
        "session_id": "s1",
        "seq": seq,
        "cls": "active_human",
        "start_utc": moscow_local(start_h),
        "end_utc": moscow_local(end_h),
        "received_at_utc": moscow_local(end_h),
    }


def close(client) -> dict:
    return client.post(f"/v1/days/{DATE}/close", params={"participant_id": "dev1"}).json()


def test_reclose_same_data_is_idempotent(client, factory):
    assert client.post("/v1/heartbeats", json=hb("e1", 10, 14, 0)).status_code == 200
    first = close(client)
    assert first["amount_cents"] == 200_000
    closed_at = None
    with factory() as s:
        rec = s.get(models.DayRecord, "partner|dev1|2026-11-12")
        closed_at = rec.closed_at
        assert rec.sha256_input == first["sha256"]

    second = close(client)
    assert second["sha256"] == first["sha256"]
    assert second["status"] == "duplicate"
    with factory() as s:
        rec = s.get(models.DayRecord, "partner|dev1|2026-11-12")
        assert rec.amount_cents == 200_000
        assert rec.paid_seconds == 4 * 3600
        assert rec.closed_at == closed_at
        assert s.query(models.Adjustment).count() == 0


def test_late_event_creates_compensation_not_overwrite(client, factory):
    assert client.post("/v1/heartbeats", json=hb("e1", 10, 14, 0)).status_code == 200
    close(client)

    # Позднее событие в грейсе: +1 ч.
    assert client.post("/v1/heartbeats", json=hb("e2", 15, 16, 1)).status_code == 200
    result = close(client)
    assert result["status"] == "compensated"
    assert result["delta_cents"] == 50_000
    assert result["recomputed_amount_cents"] == 250_000
    assert result["recomputed_paid_seconds"] == 5 * 3600

    with factory() as s:
        rec = s.get(models.DayRecord, "partner|dev1|2026-11-12")
        # Принятая запись неизменна.
        assert rec.paid_seconds == 4 * 3600
        assert rec.amount_cents == 200_000
        adj = s.query(models.Adjustment).one()
        assert adj.delta_cents == 50_000
        assert adj.approved_by == "system-late-events"
        audit = s.query(models.AuditLog).filter(
            models.AuditLog.action == "day.reclose_compensated"
        ).one()
        assert audit.old_value["amount_cents"] == 200_000
        assert audit.new_value["delta_cents"] == 50_000

    day = client.get(f"/v1/days/{DATE}", params={"participant_id": "dev1"}).json()
    assert day["paid_seconds"] == 4 * 3600
    assert day["amount_cents"] == 200_000
    assert day["adjustments_delta_cents"] == 50_000
    assert day["effective_amount_cents"] == 250_000


def test_repeat_compensation_is_idempotent(client, factory):
    client.post("/v1/heartbeats", json=hb("e1", 10, 14, 0))
    close(client)
    client.post("/v1/heartbeats", json=hb("e2", 15, 16, 1))
    first = close(client)
    second = close(client)
    assert first["status"] == "compensated"
    assert second["status"] == "duplicate_compensation"
    assert second["adjustment_id"] == first["adjustment_id"]
    with factory() as s:
        assert s.query(models.Adjustment).count() == 1


def test_cumulative_late_events_add_one_delta_each(client, factory):
    client.post("/v1/heartbeats", json=hb("e1", 10, 14, 0))
    close(client)
    client.post("/v1/heartbeats", json=hb("e2", 15, 16, 1))
    a = close(client)
    client.post("/v1/heartbeats", json=hb("e3", 16, 17, 2))
    b = close(client)
    assert a["delta_cents"] == 50_000
    assert b["delta_cents"] == 50_000  # не 100 000: дельта от эффективной суммы
    day = client.get(f"/v1/days/{DATE}", params={"participant_id": "dev1"}).json()
    assert day["adjustments_delta_cents"] == 100_000
    assert day["effective_amount_cents"] == 300_000
    with factory() as s:
        assert s.query(models.Adjustment).count() == 2


def test_digest_shows_compensation_without_mismatch(client):
    client.post("/v1/heartbeats", json=hb("e1", 10, 14, 0))
    close(client)
    client.post("/v1/heartbeats", json=hb("e2", 15, 16, 1))
    close(client)

    r = client.get(f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["paid_seconds"] == 4 * 3600
    assert d["amount_cents"] == 200_000
    assert d["adjustments_delta_cents"] == 50_000
    assert d["effective_amount_cents"] == 250_000
    assert d["recomputed_paid_seconds"] == 5 * 3600
    assert "ADR-0021" in d["text"]
