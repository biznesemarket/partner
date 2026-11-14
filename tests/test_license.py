"""Тесты M9.1 (лицензирование закрытой подписки, ADR-0018).

Сценарии:
- L-01  demo-режим: ключ не задан, REQUIRED=0 — работает, /v1/license = demo
- L-02  active: ключ валиден, срок не истек — закрытие дня работает
- L-03  grace: срок истек внутри PARTNER_LICENSE_GRACE_DAYS — закрытие
        работает, /v1/license = grace, метрика days_left отрицательная
- L-04  expired: grace прошел — закрытие дня 402 license_expired, чтение
        и explain остаются доступны (данные клиента — у клиента)
- L-05  invalid: подделанная подпись — state invalid, закрытие 402
- L-06  REQUIRED=1 без ключа — state invalid
- L-07  повторное закрытие при expired тоже 402 (гейт до пересчета)
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from partner_server import licensing, models
from partner_server.config import settings
from conftest import moscow_local

SECRET = "unit-test-delivery-secret"


def _key(tenant="fonii", edition="basic", expires="2099-01-01", secret=SECRET, tamper=False):
    k = licensing.issue_key(secret, tenant, edition, expires)
    if tamper:
        head, sig = k.rsplit(".", 1)
        k = head + "." + ("0" if sig[0] != "0" else "1") + sig[1:]
    return k


@pytest.fixture()
def license_settings():
    """Изменяемые поля лицензии с гарантией восстановления."""
    saved = {f: getattr(settings, f) for f in
             ("license_key", "license_secret", "license_required", "license_grace_days")}
    yield settings
    for f, v in saved.items():
        object.__setattr__(settings, f, v)


def _hb(client):
    r = client.post("/v1/heartbeats", json={
        "event_id": "e1", "session_id": "s1", "seq": 0, "cls": "active_human",
        "start_utc": moscow_local(0), "end_utc": moscow_local(6),
        "received_at_utc": moscow_local(6),
    })
    assert r.status_code == 200, r.text


# --- L-01 ----------------------------------------------------------------

def test_demo_mode_works(client, license_settings):
    object.__setattr__(license_settings, "license_key", "")
    object.__setattr__(license_settings, "license_required", False)
    assert client.get("/v1/license").json()["state"] == "demo"
    _hb(client)
    r = client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    assert r.status_code == 200
    assert "partner_license_days_left 0" in client.get("/v1/metrics").text


# --- L-02 ----------------------------------------------------------------

def test_active_license(client, license_settings):
    expires = (date.today() + timedelta(days=30)).isoformat()
    object.__setattr__(license_settings, "license_key", _key(expires=expires))
    object.__setattr__(license_settings, "license_secret", SECRET)
    lic = client.get("/v1/license").json()
    assert lic["state"] == "active" and lic["tenant_id"] == "fonii"
    assert 0 < lic["days_left"] <= 30
    _hb(client)
    assert client.post(
        "/v1/days/2026-11-12/close", params={"participant_id": "dev1"}
    ).status_code == 200
    assert "partner_license_days_left" in client.get("/v1/metrics").text


# --- L-03 ----------------------------------------------------------------

def test_grace_period(client, license_settings):
    # истекла 5 дней назад при грейсе 14: работает с предупреждением
    expires = (date.today() - timedelta(days=5)).isoformat()
    object.__setattr__(license_settings, "license_key", _key(expires=expires))
    object.__setattr__(license_settings, "license_secret", SECRET)
    object.__setattr__(license_settings, "license_grace_days", 14)
    assert client.get("/v1/license").json()["state"] == "grace"
    _hb(client)
    assert client.post(
        "/v1/days/2026-11-12/close", params={"participant_id": "dev1"}
    ).status_code == 200
    assert "partner_license_days_left -5" in client.get("/v1/metrics").text


# --- L-04 ----------------------------------------------------------------

def test_expired_blocks_close_not_reads(client, license_settings):
    expires = (date.today() - timedelta(days=20)).isoformat()
    object.__setattr__(license_settings, "license_key", _key(expires=expires))
    object.__setattr__(license_settings, "license_secret", SECRET)
    object.__setattr__(license_settings, "license_grace_days", 14)
    assert client.get("/v1/license").json()["state"] == "expired"
    _hb(client)
    r = client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "license_expired"
    # чтение не блокируется: данные клиента — у клиента
    assert client.get(
        "/v1/days/2026-11-12", params={"participant_id": "dev1"}
    ).status_code == 200
    assert client.get(
        "/v1/days/2026-11-12/explain", params={"participant_id": "dev1"}
    ).status_code == 200


# --- L-05 ----------------------------------------------------------------

def test_invalid_signature_blocks(client, license_settings):
    object.__setattr__(license_settings, "license_key", _key(tamper=True))
    object.__setattr__(license_settings, "license_secret", SECRET)
    lic = client.get("/v1/license").json()
    assert lic["state"] == "invalid" and lic["detail"]
    _hb(client)
    r = client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "license_invalid"


# --- L-06 ----------------------------------------------------------------

def test_required_without_key(client, license_settings):
    object.__setattr__(license_settings, "license_key", "")
    object.__setattr__(license_settings, "license_required", True)
    assert client.get("/v1/license").json()["state"] == "invalid"
    _hb(client)
    assert client.post(
        "/v1/days/2026-11-12/close", params={"participant_id": "dev1"}
    ).status_code == 402


# --- L-07 ----------------------------------------------------------------

def test_expired_reclose_still_blocked(client, factory, license_settings):
    expires = (date.today() - timedelta(days=30)).isoformat()
    object.__setattr__(license_settings, "license_key", _key(expires=expires))
    object.__setattr__(license_settings, "license_secret", SECRET)
    for _ in range(2):
        r = client.post("/v1/days/2026-11-12/close", params={"participant_id": "dev1"})
        assert r.status_code == 402
    # гейт срабатывает до пересчета: закрытых записей в БД нет
    with factory() as s:
        assert s.get(models.DayRecord, "dev1|2026-11-12") is None


# --- модуль licensing: чистые функции ---------------------------------------

def test_issue_and_parse_roundtrip():
    k = licensing.issue_key(SECRET, "acme", "pro", "2099-12-31")
    st = licensing.current_state(
        type("S", (), {"license_key": k, "license_secret": SECRET,
                       "license_required": False, "license_grace_days": 14})(),
        today=date(2099, 12, 1),
    )
    assert st.state == "active" and st.tenant_id == "acme" and st.edition == "pro"


def test_parse_rejects_wrong_secret():
    k = licensing.issue_key(SECRET, "acme", "pro", "2099-12-31")
    st = licensing.current_state(
        type("S", (), {"license_key": k, "license_secret": "other-secret",
                       "license_required": False, "license_grace_days": 14})(),
    )
    assert st.state == "invalid"
