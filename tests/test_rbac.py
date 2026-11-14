"""T-06: RBAC-роли director/participant/lawyer и аудит отказов.

Матрица:
- participant: свои устройства/дни/статус/уведомления; нельзя summary,
  flags, disputes, audit, legal, snapshots;
- director: сводка/флаги/споры/аудит/снапшоты/legal read; нельзя менять гейты;
- lawyer: legal read + гейты + snapshots read; нельзя данные времени;
- participant без participant_id — fail closed;
- неизвестная роль в PARTNER_TENANT_TOKENS — 500;
- отказ роли пишет audit action=access.denied.
"""
from __future__ import annotations

import json

import pytest

from partner_server import models, notify
from partner_server.config import settings


@pytest.fixture()
def rbac_tokens():
    old_tokens = settings.tenant_tokens_json
    old_api = settings.api_token
    object.__setattr__(
        settings,
        "tenant_tokens_json",
        json.dumps(
            {
                "tok-director": {"tenant_id": "fonii", "role": "director"},
                "tok-dev1": {
                    "tenant_id": "fonii",
                    "role": "participant",
                    "participant_id": "dev1",
                },
                "tok-dev2": {
                    "tenant_id": "fonii",
                    "role": "participant",
                    "participant_id": "dev2",
                },
                "tok-lawyer": {"tenant_id": "fonii", "role": "lawyer"},
                "tok-no-id": {"tenant_id": "fonii", "role": "participant"},
            }
        ),
    )
    object.__setattr__(settings, "api_token", "")
    try:
        yield {
            name: {"Authorization": f"Bearer {name}"}
            for name in (
                "tok-director",
                "tok-dev1",
                "tok-dev2",
                "tok-lawyer",
                "tok-no-id",
            )
        }
    finally:
        object.__setattr__(settings, "tenant_tokens_json", old_tokens)
        object.__setattr__(settings, "api_token", old_api)


@pytest.fixture()
def rbac_data(factory):
    with factory() as s:
        s.add(
            models.Participant(
                participant_id="dev2", tenant_id="fonii", display_name="Второй"
            )
        )
        s.commit()
        notify.queue(
            s,
            kind="day_digest",
            recipient="dev1",
            payload={"date": "2026-11-12"},
            text="дайджест dev1",
            tenant_id="fonii",
            project_id="partner",
        )
        notify.queue(
            s,
            kind="day_digest",
            recipient="dev2",
            payload={"date": "2026-11-12"},
            text="дайджест dev2",
            tenant_id="fonii",
            project_id="partner",
        )


@pytest.fixture()
def legal_file(tmp_path):
    old = settings.legal_file
    object.__setattr__(settings, "legal_file", str(tmp_path / "legal.yaml"))
    try:
        yield tmp_path / "legal.yaml"
    finally:
        object.__setattr__(settings, "legal_file", old)


def test_participant_sees_only_own_data(client, rbac_tokens, rbac_data):
    own = client.get(
        "/v1/devices",
        params={"participant_id": "dev1"},
        headers=rbac_tokens["tok-dev1"],
    )
    assert own.status_code == 200, own.text

    foreign = client.get(
        "/v1/devices",
        params={"participant_id": "dev2"},
        headers=rbac_tokens["tok-dev1"],
    )
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "participant_scope_denied"

    status_own = client.get("/v1/status/dev1", headers=rbac_tokens["tok-dev1"])
    assert status_own.status_code == 200, status_own.text
    status_foreign = client.get("/v1/status/dev2", headers=rbac_tokens["tok-dev1"])
    assert status_foreign.status_code == 403


def test_participant_cannot_access_director_endpoints(client, rbac_tokens, rbac_data):
    for path in (
        "/v1/status/summary",
        "/v1/flags",
        "/v1/disputes",
        "/v1/audit",
        "/v1/legal",
        "/v1/snapshots",
    ):
        response = client.get(path, headers=rbac_tokens["tok-dev1"])
        assert response.status_code == 403, (path, response.text)
        assert response.json()["detail"]["code"] == "role_denied"


def test_director_reads_but_does_not_manage_legal_gates(
    client, rbac_tokens, rbac_data, legal_file
):
    for path in (
        "/v1/status/summary",
        "/v1/flags",
        "/v1/disputes",
        "/v1/audit",
        "/v1/legal",
        "/v1/snapshots",
    ):
        response = client.get(path, headers=rbac_tokens["tok-director"])
        assert response.status_code == 200, (path, response.text)

    denied = client.post(
        "/v1/legal/gates/G-01",
        json={"status": "done", "evidence": "документ подписан", "actor": "юрист"},
        headers=rbac_tokens["tok-director"],
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "role_denied"


def test_lawyer_manages_gates_but_not_time_data(client, rbac_tokens, legal_file):
    status = client.get("/v1/legal", headers=rbac_tokens["tok-lawyer"])
    assert status.status_code == 200, status.text

    gate = client.post(
        "/v1/legal/gates/G-01",
        json={
            "status": "done",
            "evidence": "заключение юриста от 2026-11-14",
            "actor": "юрист",
        },
        headers=rbac_tokens["tok-lawyer"],
    )
    assert gate.status_code == 200, gate.text
    assert gate.json()["status"] == "done"

    assert (
        client.get("/v1/status/summary", headers=rbac_tokens["tok-lawyer"]).status_code
        == 403
    )
    assert (
        client.get(
            "/v1/days/2026-11-12",
            params={"participant_id": "dev1"},
            headers=rbac_tokens["tok-lawyer"],
        ).status_code
        == 403
    )
    assert (
        client.get("/v1/snapshots", headers=rbac_tokens["tok-lawyer"]).status_code
        == 200
    )


def test_role_denial_is_audited(client, rbac_tokens, rbac_data):
    denied = client.get("/v1/status/summary", headers=rbac_tokens["tok-dev1"])
    assert denied.status_code == 403
    audit = client.get(
        "/v1/audit", params={"entity": "api", "limit": 50}, headers=rbac_tokens["tok-director"]
    )
    assert audit.status_code == 200, audit.text
    entries = audit.json()["entries"]
    assert any(
        entry["action"] == "access.denied"
        and (entry.get("new_value") or {}).get("role") == "participant"
        for entry in entries
    )


def test_participant_notifications_are_forced_to_self(
    client, rbac_tokens, rbac_data
):
    response = client.get(
        "/v1/notifications",
        params={"recipient": "dev2"},
        headers=rbac_tokens["tok-dev1"],
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["count"] == 1
    assert {row["recipient"] for row in data["notifications"]} == {"dev1"}


def test_participant_without_id_fails_closed(client, rbac_tokens, rbac_data):
    response = client.get("/v1/notifications", headers=rbac_tokens["tok-no-id"])
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "participant_scope_denied"


def test_unknown_tenant_role_fails_closed(client):
    old = settings.tenant_tokens_json
    object.__setattr__(
        settings,
        "tenant_tokens_json",
        json.dumps({"tok-root": {"tenant_id": "fonii", "role": "root"}}),
    )
    try:
        response = client.get(
            "/v1/status/summary", headers={"Authorization": "Bearer tok-root"}
        )
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "tenant_tokens_invalid"
    finally:
        object.__setattr__(settings, "tenant_tokens_json", old)
