"""v1.2 tenant/RBAC (issue #4): токен → tenant, project_id дня, фильтры API.

Проверяем, что tenant-токен не видит проекты/дни/споры/уведомления/аудит/
снапшоты другого tenant, а внутри tenant фильтры по project_id работают.
"""
from __future__ import annotations

import json

import pytest

from conftest import moscow_local

from partner_server import models
from partner_server.config import settings


def _day(day_id: str, participant_id: str, project_id: str, date: str = "2026-11-12") -> models.DayRecord:
    return models.DayRecord(
        day_id=day_id,
        participant_id=participant_id,
        project_id=project_id,
        date=date,
        tz="Europe/Moscow",
        paid_seconds=3600,
        amount_cents=50_000,
        cash_cents=0,
        equity_cents=50_000,
        share_bp="0.5000",
        rate_version=1,
        valuation_version=1,
        policy_version=1,
        sha256_input="a" * 64,
        closed=1,
        closed_at="2026-11-13T00:00:00+00:00",
    )


@pytest.fixture()
def tenant_tokens():
    """Два tenant-токена: tok-a → fonii, tok-b → other."""
    old_tokens = settings.tenant_tokens_json
    old_api = settings.api_token
    object.__setattr__(
        settings,
        "tenant_tokens_json",
        json.dumps({"tok-a": "fonii", "tok-b": "other"}),
    )
    object.__setattr__(settings, "api_token", "")
    try:
        yield {
            "a": {"Authorization": "Bearer tok-a"},
            "b": {"Authorization": "Bearer tok-b"},
        }
    finally:
        object.__setattr__(settings, "tenant_tokens_json", old_tokens)
        object.__setattr__(settings, "api_token", old_api)


@pytest.fixture()
def other_tenant(factory):
    """Второй tenant/проект/участник/политика/сессия для проверок изоляции."""
    with factory() as s:
        s.add(models.Tenant(tenant_id="other", name="Другая организация"))
        s.add(
            models.Project(
                project_id="other-proj",
                tenant_id="other",
                name="Другой проект",
                timezone="Europe/Moscow",
            )
        )
        s.add(
            models.Participant(
                participant_id="dev2", tenant_id="other", display_name="Другой разработчик"
            )
        )
        s.add(
            models.Policy(
                policy_id="p2",
                project_id="other-proj",
                policy_version=1,
                sha256="1" * 64,
                created_at="2026-11-01T00:00:00+00:00",
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
                policy_id="p2",
                effective_from="2026-10-01",
                value_cents=50_000,
                reason="старт",
                approved_by="директор",
            )
        )
        s.add(
            models.Session(
                session_id="s2",
                participant_id="dev2",
                project_id="other-proj",
                task_ref="T-2",
                branch="dev/dev2/t2",
                started_at_utc=moscow_local(0),
            )
        )
        s.commit()
    return factory


def test_tenant_token_limits_projects_days_and_status(
    client, factory, tenant_tokens, other_tenant
):
    with factory() as s:
        s.add(_day("partner|dev1|2026-11-12", "dev1", "partner"))
        s.add(_day("other-proj|dev2|2026-11-12", "dev2", "other-proj"))
        s.commit()

    # Сводка директора: только участники своего tenant.
    a = client.get(
        "/v1/status/summary",
        params={"month": "2026-11"},
        headers=tenant_tokens["a"],
    )
    assert a.status_code == 200, a.text
    assert [p["participant_id"] for p in a.json()["participants"]] == ["dev1"]
    b = client.get(
        "/v1/status/summary",
        params={"month": "2026-11"},
        headers=tenant_tokens["b"],
    )
    assert [p["participant_id"] for p in b.json()["participants"]] == ["dev2"]

    # Чужой день/участник/проект — 403, а не 404-утечка.
    assert client.get(
        "/v1/days/2026-11-12",
        params={"participant_id": "dev2"},
        headers=tenant_tokens["a"],
    ).status_code == 403
    assert client.get(
        "/v1/days/2026-11-12",
        params={"participant_id": "dev2", "project_id": "other-proj"},
        headers=tenant_tokens["a"],
    ).status_code == 403
    assert client.get("/v1/status/dev2", headers=tenant_tokens["a"]).status_code == 403
    assert client.get(
        "/v1/status/dev1",
        params={"project_id": "other-proj"},
        headers=tenant_tokens["a"],
    ).status_code == 403


def test_tenant_header_cannot_override_bound_token(client, tenant_tokens):
    r = client.get(
        "/v1/status/summary",
        headers={**tenant_tokens["a"], "X-Partner-Tenant": "other"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "cross_tenant_denied"


def test_status_summary_and_status_filter_project_within_tenant(client, factory):
    """Внутри одного tenant summary/status не смешивают проекты."""
    with factory() as s:
        s.add(
            models.Project(
                project_id="partner2",
                tenant_id="fonii",
                name="Второй проект",
                timezone="Europe/Moscow",
            )
        )
        s.add(
            models.Participant(
                participant_id="dev3", tenant_id="fonii", display_name="Третий"
            )
        )
        s.add(_day("partner|dev1|2026-11-12", "dev1", "partner"))
        s.add(_day("partner2|dev3|2026-11-12", "dev3", "partner2"))
        s.commit()

    r = client.get(
        "/v1/status/summary", params={"month": "2026-11", "project_id": "partner"}
    )
    assert [p["participant_id"] for p in r.json()["participants"]] == ["dev1"]
    r2 = client.get(
        "/v1/status/summary", params={"month": "2026-11", "project_id": "partner2"}
    )
    assert [p["participant_id"] for p in r2.json()["participants"]] == ["dev3"]

    # status участника фильтруется по project_id.
    assert client.get(
        "/v1/status/dev3", params={"project_id": "partner"}
    ).json()["amount_cents_month"] == 0


def test_notifications_and_audit_filtered_by_tenant(client, factory, tenant_tokens, other_tenant):
    with factory() as s:
        s.add(
            models.Notification(
                notification_id="ntf-fonii",
                tenant_id="fonii",
                project_id="partner",
                kind="day_digest",
                recipient="dev1",
                status="sent",
                payload_json={},
                created_at="2026-11-13T00:00:00+00:00",
            )
        )
        s.add(
            models.Notification(
                notification_id="ntf-other",
                tenant_id="other",
                project_id="other-proj",
                kind="day_digest",
                recipient="dev2",
                status="sent",
                payload_json={},
                created_at="2026-11-13T00:00:00+00:00",
            )
        )
        s.add(
            models.AuditLog(
                audit_id="aud-fonii",
                tenant_id="fonii",
                project_id="partner",
                ts="2026-11-13T00:00:00+00:00",
                actor="director",
                entity="policies",
                entity_id="p1",
                action="policy.create",
            )
        )
        s.add(
            models.AuditLog(
                audit_id="aud-other",
                tenant_id="other",
                project_id="other-proj",
                ts="2026-11-13T00:00:00+00:00",
                actor="director",
                entity="policies",
                entity_id="p2",
                action="policy.create",
            )
        )
        s.commit()

    n_a = client.get("/v1/notifications", headers=tenant_tokens["a"]).json()
    assert [n["notification_id"] for n in n_a["notifications"]] == ["ntf-fonii"]
    n_b = client.get("/v1/notifications", headers=tenant_tokens["b"]).json()
    assert [n["notification_id"] for n in n_b["notifications"]] == ["ntf-other"]

    au_a = client.get("/v1/audit", headers=tenant_tokens["a"]).json()
    assert [e["audit_id"] for e in au_a["entries"]] == ["aud-fonii"]
    assert client.get(
        "/v1/audit", params={"project_id": "other-proj"}, headers=tenant_tokens["a"]
    ).status_code == 403
    assert client.get(
        "/v1/notifications",
        params={"project_id": "other-proj"},
        headers=tenant_tokens["a"],
    ).status_code == 403


def test_snapshots_filtered_by_tenant(client, factory, tenant_tokens, other_tenant):
    with factory() as s:
        s.add(
            models.Snapshot(
                snapshot_id="snap-partner-2026-11",
                month="2026-11",
                sha256="a" * 64,
                created_at="2026-11-30T00:00:00+00:00",
                payload_json={"tenant_id": "fonii", "project_id": "partner"},
            )
        )
        s.add(
            models.Snapshot(
                snapshot_id="snap-other-proj-2026-11",
                month="2026-11",
                sha256="b" * 64,
                created_at="2026-11-30T00:00:00+00:00",
                payload_json={"tenant_id": "other", "project_id": "other-proj"},
            )
        )
        s.commit()

    a = client.get("/v1/snapshots", headers=tenant_tokens["a"]).json()
    assert [s["snapshot_id"] for s in a["snapshots"]] == ["snap-partner-2026-11"]
    b = client.get("/v1/snapshots", headers=tenant_tokens["b"]).json()
    assert [s["snapshot_id"] for s in b["snapshots"]] == ["snap-other-proj-2026-11"]
    assert client.get(
        "/v1/snapshots/snap-other-proj-2026-11", headers=tenant_tokens["a"]
    ).status_code == 404


def test_session_and_heartbeat_cross_tenant_denied(
    client, tenant_tokens, other_tenant
):
    r = client.post(
        "/v1/sessions",
        headers=tenant_tokens["a"],
        json={
            "session_id": "s-cross",
            "participant_id": "dev2",
            "project_id": "other-proj",
            "started_at_utc": moscow_local(0),
        },
    )
    assert r.status_code == 403

    hb = {
        "event_id": "hb-cross",
        "session_id": "s2",
        "seq": 0,
        "cls": "active_human",
        "start_utc": moscow_local(10),
        "end_utc": moscow_local(11),
        "received_at_utc": moscow_local(11),
    }
    r = client.post("/v1/heartbeats", json=hb, headers=tenant_tokens["a"])
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "cross_tenant_denied"


def test_close_day_stores_project_scoped_day_id(client, factory):
    hb = {
        "event_id": "e-tenant-day",
        "session_id": "s1",
        "seq": 0,
        "cls": "active_human",
        "start_utc": moscow_local(10),
        "end_utc": moscow_local(14),
        "received_at_utc": moscow_local(14),
    }
    assert client.post("/v1/heartbeats", json=hb).status_code == 200
    closed = client.post(
        "/v1/days/2026-11-12/close", params={"participant_id": "dev1"}
    ).json()
    assert closed["day_id"] == "partner|dev1|2026-11-12"

    with factory() as s:
        rec = s.get(models.DayRecord, "partner|dev1|2026-11-12")
        assert rec is not None
        assert rec.project_id == "partner"
        # Legacy-идентификатор не создается для новых дней.
        assert s.get(models.DayRecord, "dev1|2026-11-12") is None


def test_legacy_day_record_is_readable_but_not_mixed(client, factory):
    """Исторический day_id participant|date читается только своим проектом."""
    with factory() as s:
        s.add(_day("dev1|2026-11-12", "dev1", "partner"))
        s.commit()

    r = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"})
    assert r.status_code == 200
    assert r.json()["closed"] is True

    # Другой проект того же tenant не должен видеть legacy-день.
    with factory() as s:
        s.add(
            models.Project(
                project_id="partner2",
                tenant_id="fonii",
                name="Второй проект",
                timezone="Europe/Moscow",
            )
        )
        s.add(
            models.Policy(
                policy_id="p-partner2",
                project_id="partner2",
                policy_version=1,
                sha256="2" * 64,
                created_at="2026-11-01T00:00:00+00:00",
                params_json={"rate_rub_per_hour": 500, "valuation_rub": 10_000_000},
            )
        )
        s.commit()
    r = client.get(
        "/v1/days/2026-11-12",
        params={"participant_id": "dev1", "project_id": "partner2"},
    )
    assert r.status_code == 200
    assert r.json()["closed"] is False

def test_flags_scoped_by_tenant_and_project(
    client, factory, tenant_tokens, other_tenant
):
    """Флаги дашборда не смешивают tenant/проекты (issue #10)."""
    with factory() as s:
        s.add(_day("partner|dev1|2026-11-12", "dev1", "partner"))
        s.add(models.Flag(day_id="partner|dev1|2026-11-12", code="no_output"))
        s.add(_day("other-proj|dev2|2026-11-12", "dev2", "other-proj"))
        s.add(models.Flag(day_id="other-proj|dev2|2026-11-12", code="clock_skew"))
        s.commit()

    a = client.get("/v1/flags", headers=tenant_tokens["a"])
    assert a.status_code == 200, a.text
    assert [(f["participant_id"], f["code"]) for f in a.json()["flags"]] == [
        ("dev1", "no_output")
    ]
    b = client.get("/v1/flags", headers=tenant_tokens["b"])
    assert [(f["participant_id"], f["code"]) for f in b.json()["flags"]] == [
        ("dev2", "clock_skew")
    ]

    # Чужой проект нельзя запросить даже с явным project_id.
    assert (
        client.get(
            "/v1/flags",
            params={"project_id": "other-proj"},
            headers=tenant_tokens["a"],
        ).status_code
        == 403
    )
