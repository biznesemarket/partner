"""Тесты M9 (threat-model §4): подпись устройств источника heartbeats.

Сценарии:
- D-01  регистрация устройства: секрет выдается один раз; список без секретов
- D-02  heartbeat с корректной подписью HMAC — принимается
- D-03  неверная подпись → 403 device_signature_invalid + инцидент в аудит
- D-04  неизвестное/отозванное устройство → 403 device_invalid
- D-05  PARTNER_REQUIRE_DEVICE_SIGN=1: неподписанный heartbeat → 422
- D-06  подпись без устройства (только X-Partner-Sign) → 422
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod

from partner_server import models
from partner_server.config import settings
from tests.conftest import moscow_local


def _payload(seq=0):
    return {
        "event_id": f"e{seq}",
        "session_id": "s1",
        "seq": seq,
        "cls": "active_human",
        "start_utc": moscow_local(0),
        "end_utc": moscow_local(2),
        "received_at_utc": moscow_local(2),
    }


def _sign(secret: str, p: dict) -> str:
    line = f"{p['event_id']}|{p['session_id']}|{p['seq']}|{p['start_utc']}|{p['end_utc']}"
    return hmac_mod.new(secret.encode(), line.encode(), hashlib.sha256).hexdigest()


def _register(client, label="ноутбук dev1"):
    r = client.post("/v1/devices", json={"participant_id": "dev1", "label": label})
    assert r.status_code == 201, r.text
    return r.json()


# --- D-01 ----------------------------------------------------------------

def test_register_device_shows_secret_once(client, factory):
    d = _register(client)
    assert d["secret"] and len(d["secret"]) == 64
    lst = client.get("/v1/devices", params={"participant_id": "dev1"}).json()
    assert len(lst) == 1
    assert "secret" not in lst[0]
    assert lst[0]["status"] == "active"


# --- D-02 ----------------------------------------------------------------

def test_signed_heartbeat_accepted(client):
    d = _register(client)
    p = _payload()
    r = client.post(
        "/v1/heartbeats",
        json=p,
        headers={"X-Partner-Device": d["device_id"], "X-Partner-Sign": _sign(d["secret"], p)},
    )
    assert r.status_code == 200, r.text


# --- D-03 ----------------------------------------------------------------

def test_wrong_signature_rejected(client, factory):
    d = _register(client)
    p = _payload()
    r = client.post(
        "/v1/heartbeats",
        json=p,
        headers={"X-Partner-Device": d["device_id"],
                 "X-Partner-Sign": "0" * 64},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "device_signature_invalid"
    with factory() as s:
        codes = [row.action for row in s.query(models.AuditLog).all()]
        assert "incident.device_signature_invalid" in codes


# --- D-04 ----------------------------------------------------------------

def test_unknown_and_revoked_device(client, factory):
    p = _payload()
    r = client.post(
        "/v1/heartbeats",
        json=p,
        headers={"X-Partner-Device": "dev-nope", "X-Partner-Sign": "1" * 64},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "device_invalid"

    d = _register(client)
    assert client.post(f"/v1/devices/{d['device_id']}/revoke").status_code == 200
    r = client.post(
        "/v1/heartbeats",
        json=p,
        headers={"X-Partner-Device": d["device_id"], "X-Partner-Sign": _sign(d["secret"], p)},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "device_invalid"


# --- D-05 ----------------------------------------------------------------

def test_required_sign_mode_rejects_unsigned(client, factory):
    saved = settings.require_device_sign
    object.__setattr__(settings, "require_device_sign", True)
    try:
        r = client.post("/v1/heartbeats", json=_payload())
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "device_signature_required"
    finally:
        object.__setattr__(settings, "require_device_sign", saved)


# --- D-06 ----------------------------------------------------------------

def test_sign_without_device_id_rejected(client):
    r = client.post("/v1/heartbeats", json=_payload(), headers={"X-Partner-Sign": "a" * 64})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "device_signature_required"
