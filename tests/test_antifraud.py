"""Красные тесты антифрода M5 (development-plan.md §5.5, threat-model.md §5).

Каждый тест = атака из модели угроз и ожидаемая мера:
- replay (T-3): повтор не записывается, инцидент в аудите;
- night_autonomous_spike (T-1): ночь помечена, деньги ограничены капами;
- synthetic_input (T-2): равномерная серия помечена;
- idle_no_signal: супервизия без сигналов → простой;
- clock_skew (T-4) и late_rejected (T-09);
- кросс-тенантный запрос → 403 + инцидент (изоляция §4);
- заднее число версии ставки → 422 version_backdated (R-2, V-04).
"""
from __future__ import annotations

from conftest import moscow_local
from datetime import datetime, timedelta, timezone

from partner_server import audit as audit_mod
from partner_server import models
from partner_server import service

DATE = "2026-11-12"


def _hb(event, cls, start_h, end_h, seq, *, signals=None, received_offset_s=1,
        date_offset=0):
    end_iso = moscow_local(end_h, date_offset_days=date_offset)
    received = (
        datetime.fromisoformat(end_iso) + timedelta(seconds=received_offset_s)
    ).isoformat(timespec="seconds")
    return {
        "event_id": event,
        "session_id": "s1",
        "seq": seq,
        "cls": cls,
        "start_utc": moscow_local(start_h, date_offset_days=date_offset),
        "end_utc": end_iso,
        "received_at_utc": received,
        "signals": signals,
    }


def _incidents(client, code):
    entries = client.get("/v1/audit", params={"limit": 500}).json()["entries"]
    return [e for e in entries if e["action"] == f"incident.{code}"]


# ---------- T-3: replay ----------


def test_replay_returns_duplicate_and_logs_incident(client):
    r1 = client.post("/v1/heartbeats", json=_hb("rp1", "active_human", 10, 12, 0))
    assert r1.json()["status"] == "accepted"
    r2 = client.post("/v1/heartbeats", json=_hb("rp1", "active_human", 10, 12, 0))
    assert r2.json() == {"status": "duplicate", "event_id": "rp1"}

    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 100_000  # интервал один, не два

    incidents = _incidents(client, "replay")
    assert len(incidents) == 1
    assert incidents[0]["entity_id"] == "rp1"


def test_duplicate_seq_logs_incident(client):
    client.post("/v1/heartbeats", json=_hb("ds1", "active_human", 10, 12, 0))
    r = client.post(
        "/v1/heartbeats",
        json=_hb("ds2", "active_human", 12, 13, 0),  # тот же seq
    )
    assert r.status_code == 422
    assert _incidents(client, "duplicate_seq")


# ---------- T-1: ночная накрутка ----------


def test_night_autonomous_spike_flagged_but_capped(client):
    """Утро 00–06 (6 ч) + вечер 22–24 (2 ч) = 8 ч автономии за ночь → флаг; ≤ 1 000 ₽."""
    client.post("/v1/heartbeats", json=_hb("ns1a", "autonomous_agent", 0, 6, 0))
    client.post("/v1/heartbeats", json=_hb("ns1b", "autonomous_agent", 22, 24, 1))
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert "night_autonomous_spike" in day["flags"]
    # k_autonomous 0,2: 8 ч → 1,6 оплач. ч × 500 ₽ = 800 ₽ (ночь не может дать больше)
    assert day["amount_cents"] == 80_000


def test_no_night_flag_under_threshold(client):
    """6 ч автономии ровно в ночном окне → порог не превышен (>6 ч), флага нет."""
    client.post("/v1/heartbeats", json=_hb("ns2", "autonomous_agent", 0, 6, 0))
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert "night_autonomous_spike" not in day["flags"]
    assert day["amount_cents"] == 60_000  # 1,2 оплач. ч × 500 ₽


# ---------- T-2: синтетический ввод ----------


def test_synthetic_input_uniform_series_flagged(client):
    """5 подряд одинаковых 1-часовых супервизий без вариативности → synthetic_input."""
    for i in range(5):
        client.post(
            "/v1/heartbeats",
            json=_hb(f"sy{i}", "supervised_agent", 9 + i, 10 + i, i,
                     signals={"input_events": 42, "focus": 7}),
        )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert "synthetic_input" in day["flags"]
    # супервизия не блокируется: 5 ч × 500 ₽
    assert day["amount_cents"] == 250_000


def test_varied_series_not_flagged(client):
    """Разная длительность интервалов — вариативность есть, флага нет."""
    for i, (s, e) in enumerate([(9, 11), (11, 12), (12, 13), (13, 14)]):
        client.post(
            "/v1/heartbeats",
            json=_hb(f"vr{i}", "supervised_agent", s, e, i,
                     signals={"input_events": 10 + i, "focus": 3}),
        )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert "synthetic_input" not in day["flags"]


# ---------- idle_no_signal ----------


def test_supervised_without_signals_counts_as_idle(client):
    """Супервизия с нулевыми сигналами → простой (0 ₽), не оплачиваемое время."""
    client.post(
        "/v1/heartbeats",
        json=_hb("id1", "supervised_agent", 10, 12, 0,
                 signals={"input_events": 0, "focus": 0}),
    )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 0
    assert "idle_no_signal" in day["flags"]


# ---------- T-4 / T-09: сдвиг часов и опоздавшие события ----------


def test_clock_skew_flagged(client):
    """Конец интервала на 10 мин позже приема → clock_skew (серверное время авторитетно)."""
    client.post(
        "/v1/heartbeats",
        json=_hb("cs1", "active_human", 10, 12, 0, received_offset_s=-600),
    )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert "clock_skew" in day["flags"]


def test_late_beyond_grace_not_paid(client):
    """T-09: прием через 30 ч после конца интервала → late_rejected, 0 ₽."""
    client.post("/v1/sessions", json={  # заглушка для читаемости
        "session_id": "s-x", "participant_id": "dev1", "project_id": "partner",
        "task_ref": "X", "started_at_utc": moscow_local(0),
    })
    end_iso = moscow_local(12)
    late = (
        datetime.fromisoformat(end_iso) + timedelta(hours=30)
    ).isoformat(timespec="seconds")
    r = client.post(
        "/v1/heartbeats",
        json={
            "event_id": "lt1", "session_id": "s1", "seq": 0,
            "cls": "active_human",
            "start_utc": moscow_local(10), "end_utc": end_iso,
            "received_at_utc": late,
        },
    )
    assert r.json()["status"] == "accepted"  # принят, но не оплачен
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 0
    assert "late_rejected" in day["flags"]


def test_late_within_grace_paid(client):
    """T-08: прием через 5 ч → в грейсе, оплачивается."""
    end_iso = moscow_local(12)
    soon = (
        datetime.fromisoformat(end_iso) + timedelta(hours=5)
    ).isoformat(timespec="seconds")
    client.post(
        "/v1/heartbeats",
        json={
            "event_id": "lt2", "session_id": "s1", "seq": 0,
            "cls": "active_human",
            "start_utc": moscow_local(10), "end_utc": end_iso,
            "received_at_utc": soon,
        },
    )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 100_000


# ---------- §4: изоляция tenants ----------


def test_cross_tenant_read_denied_403_and_incident(client, factory):
    with factory() as s:
        s.add(models.Tenant(tenant_id="rival", name="Конкурент"))
        s.add(
            models.Participant(
                participant_id="spy", tenant_id="rival", display_name="Шпион"
            )
        )
        s.commit()

    r = client.get("/v1/days/2026-11-12", params={"participant_id": "spy"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "cross_tenant_denied"
    incidents = _incidents(client, "cross_tenant_denied")
    assert len(incidents) == 1
    assert incidents[0]["meta"]["participant_tenant"] == "rival"


def test_cross_tenant_session_denied(client, factory):
    with factory() as s:
        s.add(models.Tenant(tenant_id="rival", name="Конкурент"))
        s.add(
            models.Participant(
                participant_id="spy", tenant_id="rival", display_name="Шпион"
            )
        )
        s.commit()
    r = client.post(
        "/v1/sessions",
        json={
            "session_id": "ss", "participant_id": "spy",
            "project_id": "partner", "started_at_utc": moscow_local(0),
        },
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "cross_tenant_denied"


# ---------- R-2 / V-04: заднее число версии ставки ----------


def test_rate_version_backdated_rejected(client):
    r = client.post(
        "/v1/rate-versions",
        json={
            "policy_id": "p1", "effective_from": "2020-01-01",
            "value_cents": 90_000, "reason": "попытка задним числом",
            "approved_by": "директор",
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "version_backdated"
    assert _incidents(client, "rate_backdated")


def test_rate_version_future_accepted(client):
    r = client.post(
        "/v1/rate-versions",
        json={
            "policy_id": "p1", "effective_from": "2027-01-01",
            "value_cents": 60_000, "reason": "индексация",
            "approved_by": "директор",
        },
    )
    assert r.status_code == 201
    assert r.json()["status"] == "created"


# ---------- M5.4: аудит политики ----------


def test_policy_version_bump_recorded_in_audit(client):
    r = client.post(
        "/v1/policies",
        json={
            "project_id": "partner",
            "params_json": {"rate_rub_per_hour": 600, "valuation_rub": 12_000_000},
            "approved_by": "директор",
            "reason": "индексация ставки",
        },
    )
    assert r.status_code == 201
    assert r.json()["policy_version"] == 2

    entries = client.get("/v1/audit", params={"entity": "policies"}).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["action"] == "policy.create"
    assert entries[0]["actor"] == "директор"
    assert entries[0]["meta"]["version"] == 2

    # новая политика действует для расчета: ставка 600 → T-01-день стал бы 3 960 ₽
    client.post("/v1/heartbeats", json=_hb("np1", "active_human", 10, 12, 0))
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == 120_000  # 2 ч × 600 ₽
    assert day["policy_version"] == 2


def test_service_level_replay_incident(factory):
    """Сервисный уровень: прямой вызов ingest повтором тоже пишет инцидент."""
    from datetime import datetime as dt

    common = dict(
        session_id="s1", seq=0, cls="active_human",
        start_utc=moscow_local(10), end_utc=moscow_local(12),
        received_at=dt.now(timezone.utc),
    )
    with factory() as sess:
        a = service.ingest_heartbeat(sess, event_id="sv1", **common)
        b = service.ingest_heartbeat(sess, event_id="sv1", **common)
        assert a["status"] == "accepted" and b["status"] == "duplicate"
        entries = audit_mod.list_entries(sess, entity="heartbeats")
        assert any(e.action == "incident.replay" for e in entries)
