"""Тесты partner-agent-kit (issue #1).

Сценарии контракта docs/03-architecture/agent-kit.md §7:
- подпись устройства сходится с сервером;
- офлайн-буфер отправляет события в порядке seq;
- повтор event_id идемпотентен, повтор seq → 422;
- интервал режется по локальной полуночи;
- сессия без task_ref → no_task_link;
- сигналы не содержат кода/промптов;
- MCP-инструменты и подтверждение человека.
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from partner_agent_kit.buffer import OfflineBuffer
from partner_agent_kit.client import (
    PartnerClient,
    PartnerHTTPError,
    PartnerOfflineError,
)
from partner_agent_kit.config import AgentConfig, load_config, save_config
from partner_agent_kit.daemon import run_daemon
from partner_agent_kit.mcp_server import MCPServer
from partner_agent_kit.session import (
    AgentSession,
    classify,
    sanitize_signals,
    split_at_midnight,
)
from partner_server import models

MSK = timezone(timedelta(hours=3))
START = datetime(2026, 11, 12, 10, 0, tzinfo=MSK)


class FakeClock:
    def __init__(self, start: datetime = START):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class HttpTestTransport:
    """Транспорт PartnerClient поверх FastAPI TestClient (без сокета)."""

    def __init__(self, test_client, *, online: bool = True):
        self.test_client = test_client
        self.online = online
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    def __call__(self, method, url, body, headers, timeout):
        payload = json.loads(body) if body else None
        self.calls.append((method, url, payload, headers))
        if not self.online:
            raise PartnerOfflineError("сеть недоступна (тест)")
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        response = self.test_client.request(method, path, content=body, headers=headers)
        try:
            data = response.json()
        except ValueError:
            data = {}
        return response.status_code, data


def _config(tmp_path: pathlib.Path, **overrides) -> AgentConfig:
    params = dict(
        server_url="http://testserver",
        participant_id="dev1",
        project_id="partner",
        timezone="Europe/Moscow",
        buffer_path=str(tmp_path / "agent-buffer.jsonl"),
    )
    params.update(overrides)
    return AgentConfig(**params)


def _register_device(client) -> dict:
    response = client.post("/v1/devices", json={"participant_id": "dev1", "label": "тест"})
    assert response.status_code == 201, response.text
    return response.json()


def _client(cfg: AgentConfig, transport: HttpTestTransport) -> PartnerClient:
    return PartnerClient(cfg.server_url, cfg.api_token, transport=transport)


# ---------- конфиг ----------


def test_config_roundtrip_and_redaction(tmp_path):
    cfg = _config(tmp_path, api_token="tok", device_id="dev-1", device_secret="sec")
    path = tmp_path / "agent.yaml"
    saved = save_config(cfg, path)
    assert saved == path
    loaded = load_config(path)
    assert loaded.participant_id == "dev1"
    assert loaded.device_secret == "sec"
    assert loaded.redacted()["api_token"] == "***"
    assert loaded.redacted()["device_secret"] == "***"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------- подпись и прием ----------


def test_signed_heartbeat_accepted_and_replay_is_idempotent(client, factory, tmp_path):
    device = _register_device(client)
    clock = FakeClock()
    cfg = _config(tmp_path, device_id=device["device_id"], device_secret=device["secret"])
    transport = HttpTestTransport(client)
    session = AgentSession(cfg, client=_client(cfg, transport), clock=clock)
    session_id = session.start(task_ref="T-1", branch="dev/dev1/T-1")
    assert session_id.startswith("ses-")

    clock.advance(30)
    events = session.heartbeat(
        {"agent_running": True, "focus": 1, "tool_calls": 2}, end_at=clock()
    )
    assert len(events) == 1
    assert events[0]["cls"] == "supervised_agent"

    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["paid_seconds"] in (29, 30)

    first = _client(cfg, transport).send_heartbeat(
        events[0], device_id=cfg.device_id, device_secret=cfg.device_secret
    )
    assert first == {"status": "duplicate", "event_id": events[0]["event_id"]}


def test_wrong_device_secret_rejected(client, tmp_path):
    device = _register_device(client)
    client.post(
        "/v1/sessions",
        json={
            "session_id": "s-sign", "participant_id": "dev1", "project_id": "partner",
            "task_ref": "T-1", "branch": "dev/dev1/T-1",
            "started_at_utc": START.astimezone(timezone.utc).isoformat(),
        },
    )
    transport = HttpTestTransport(client)
    cfg = _config(tmp_path, device_id=device["device_id"], device_secret="0" * 64)
    event = {
        "event_id": "evt-bad-sign", "session_id": "s-sign", "seq": 0,
        "cls": "active_human",
        "start_utc": "2026-11-12T07:00:00+00:00",
        "end_utc": "2026-11-12T07:00:30+00:00",
        "received_at_utc": "2026-11-12T07:00:31+00:00",
    }
    with pytest.raises(PartnerHTTPError) as err:
        _client(cfg, transport).send_heartbeat(
            event, device_id=cfg.device_id, device_secret=cfg.device_secret
        )
    assert err.value.status == 403
    assert err.value.code == "device_signature_invalid"


def test_duplicate_seq_rejected(client, tmp_path):
    device = _register_device(client)
    transport = HttpTestTransport(client)
    cfg = _config(tmp_path, device_id=device["device_id"], device_secret=device["secret"])
    client_obj = _client(cfg, transport)
    client.post(
        "/v1/sessions",
        json={
            "session_id": "s-seq", "participant_id": "dev1", "project_id": "partner",
            "task_ref": "T-1", "branch": "dev/dev1/T-1",
            "started_at_utc": START.astimezone(timezone.utc).isoformat(),
        },
    )
    base = {
        "event_id": "evt-a", "session_id": "s-seq", "seq": 0,
        "cls": "active_human",
        "start_utc": "2026-11-12T07:00:00+00:00",
        "end_utc": "2026-11-12T07:00:30+00:00",
        "received_at_utc": "2026-11-12T07:00:31+00:00",
    }
    assert client_obj.send_heartbeat(
        base, device_id=cfg.device_id, device_secret=cfg.device_secret
    )["status"] == "accepted"
    with pytest.raises(PartnerHTTPError) as err:
        client_obj.send_heartbeat(
            base | {"event_id": "evt-b"},
            device_id=cfg.device_id,
            device_secret=cfg.device_secret,
        )
    assert err.value.status == 422
    assert err.value.code == "duplicate_seq"


# ---------- офлайн-буфер ----------


def test_offline_buffer_replays_in_seq_order(client, factory, tmp_path):
    device = _register_device(client)
    cfg = _config(tmp_path, device_id=device["device_id"], device_secret=device["secret"])
    transport = HttpTestTransport(client, online=False)
    clock = FakeClock()
    session = AgentSession(cfg, client=_client(cfg, transport), clock=clock)
    session.start(task_ref="T-1", branch="dev/dev1/T-1")
    clock.advance(30)
    session.heartbeat({"agent_running": True, "focus": 1})
    clock.advance(30)
    session.heartbeat({"agent_running": True, "focus": 1})
    assert len(session.buffer) == 3  # session + два heartbeat

    transport.online = True
    result = session.flush()
    assert result == {"sent": 3, "failed": [], "pending": 0}
    assert len(session.buffer) == 0

    with factory() as db:
        session_row = db.get(models.Session, session.session_id)
        assert session_row is not None
        seqs = [
            row.seq
            for row in db.query(models.Heartbeat)
            .filter(models.Heartbeat.session_id == session.session_id)
            .order_by(models.Heartbeat.seq)
            .all()
        ]
        assert seqs == [0, 1]
    assert [call[2]["seq"] for call in transport.calls if call[2] and "seq" in call[2]] == [0, 1]


def test_offline_buffer_keeps_events_when_server_still_down(tmp_path):
    buffer = OfflineBuffer(tmp_path / "buf.jsonl")
    buffer.append({"kind": "heartbeat", "event_id": "e1", "payload": {"event_id": "e1"}})

    def offline(_item):
        raise PartnerOfflineError("нет сети")

    result = buffer.flush(offline)
    assert result["pending"] == 1
    assert len(OfflineBuffer(tmp_path / "buf.jsonl")) == 1


# ---------- разрез суток и классы ----------


def test_split_interval_at_local_midnight():
    start = datetime(2026, 11, 12, 23, 50, tzinfo=MSK)
    end = datetime(2026, 11, 13, 0, 10, tzinfo=MSK)
    segments = split_at_midnight(start, end, "Europe/Moscow")
    assert len(segments) == 2
    assert segments[0][1].isoformat() == "2026-11-12T21:00:00+00:00"
    assert segments[1][0].isoformat() == "2026-11-12T21:00:00+00:00"


def test_midnight_crossing_heartbeat_lands_in_two_days(client, tmp_path):
    device = _register_device(client)
    transport = HttpTestTransport(client)
    cfg = _config(tmp_path, device_id=device["device_id"], device_secret=device["secret"])
    clock = FakeClock(datetime(2026, 11, 12, 23, 50, tzinfo=MSK))
    session = AgentSession(cfg, client=_client(cfg, transport), clock=clock)
    session.start(task_ref="T-1", branch="dev/dev1/T-1")
    clock.advance(20 * 60)
    events = session.heartbeat({"input_events": 2, "focus": 1}, end_at=clock())
    assert len(events) == 2
    assert [event["seq"] for event in events] == [0, 1]

    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    next_day = client.get("/v1/days/2026-11-13", params={"participant_id": "dev1"}).json()
    assert day["paid_seconds"] in (599, 600)
    assert next_day["paid_seconds"] in (599, 600)


def test_classification_and_signal_whitelist():
    assert classify({}, agent_attached=True) == "idle"
    assert classify({"agent_running": True}, agent_attached=True) == "autonomous_agent"
    assert (
        classify({"agent_running": True, "focus": 1}, agent_attached=True)
        == "supervised_agent"
    )
    assert classify({"input_events": 3}, agent_attached=False) == "active_human"
    assert (
        classify({"agent_running": True, "focus": 1}, agent_attached=True, idle_sec=400)
        == "idle"
    )
    clean = sanitize_signals(
        {"input_events": 2, "prompt": "секрет", "code": "print(1)", "agent_running": 1}
    )
    assert clean == {"input_events": 2, "agent_running": True}
    assert "prompt" not in clean and "code" not in clean


def test_session_without_task_ref_is_not_paid(client, tmp_path):
    transport = HttpTestTransport(client)
    cfg = _config(tmp_path)
    clock = FakeClock()
    session = AgentSession(cfg, client=_client(cfg, transport), clock=clock)
    session.start(branch="dev/dev1/no-task")
    clock.advance(3600)
    session.heartbeat({"input_events": 5, "focus": 1}, end_at=clock())
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["paid_seconds"] == 0
    assert "no_task_link" in day["flags"]


# ---------- MCP ----------


def test_mcp_tools_and_human_confirmation(client, tmp_path):
    transport = HttpTestTransport(client)
    cfg = _config(tmp_path)
    clock = FakeClock()
    mcp = MCPServer(cfg, client=_client(cfg, transport), clock=clock)

    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    names = {
        tool["name"] for tool in mcp.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )["result"]["tools"]
    }
    assert names == {
        "partner_start_session", "partner_heartbeat", "partner_status",
        "partner_day", "partner_explain", "partner_dispute",
    }

    denied = mcp.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "partner_start_session", "arguments": {"task_ref": "T-1"}},
    })
    assert denied["result"]["isError"] is True

    started = mcp.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "partner_start_session",
                   "arguments": {"task_ref": "T-1", "branch": "dev/dev1/T-1",
                                 "confirm": True}},
    })
    assert "isError" not in started["result"]
    clock.advance(30)
    beat = mcp.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "partner_heartbeat", "arguments": {"signals": {"focus": 1}}},
    })
    assert "isError" not in beat["result"]
    too_soon = mcp.handle({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "partner_heartbeat", "arguments": {}},
    })
    assert too_soon["result"]["isError"] is True

    status = mcp.handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "partner_status", "arguments": {}},
    })
    assert "dev1" in status["result"]["content"][0]["text"]


# ---------- daemon ----------


def test_daemon_fake_clock(client, factory, tmp_path):
    cfg = _config(tmp_path)
    transport = HttpTestTransport(client)
    clock = FakeClock()
    session = AgentSession(cfg, client=_client(cfg, transport), clock=clock)
    result = run_daemon(
        cfg,
        task_ref="T-1",
        branch="dev/dev1/T-1",
        session=session,
        provider=lambda: {"agent_running": True, "focus": 1},
        clock=clock,
        sleep=clock.advance,
        iterations=3,
    )
    assert result["ticks"] == 3
    assert result["heartbeats"] == 2  # первый тик — нулевой интервал
    with factory() as db:
        assert db.get(models.Session, session.session_id) is not None
