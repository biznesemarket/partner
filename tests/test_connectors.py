"""Тесты коннекторов GitHub/Forgejo (issue #2) и флага no_output (issue #3).

Сценарии:
- HMAC-подпись вебхука: missing → 401, invalid → 403 + аудит, нет секрета → 503;
- push/pull_request/issues → факты repo_facts, дедупликация delivery_id;
- привязка сессии к задаче по ветке dev/<логин>/<тема>;
- Forgejo-подпись и импорт фактов из API;
- no_output: >3 оплач. ч без коммитов/диффов/tool_calls.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from conftest import moscow_local
from partner_server import models
from partner_server.config import settings

SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def connector_secret():
    object.__setattr__(settings, "connector_secret", SECRET)
    object.__setattr__(settings, "connector_secrets_json", "")
    yield SECRET


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def post_webhook(
    client,
    *,
    forge: str = "github",
    event: str = "push",
    payload: dict,
    delivery: str = "delivery-1",
    secret: str = SECRET,
    sign: bool = True,
    project_id: str | None = None,
    signature_header: str | None = None,
):
    raw = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if forge == "github":
        headers["X-GitHub-Event"] = event
        headers["X-GitHub-Delivery"] = delivery
        if sign:
            headers["X-Hub-Signature-256"] = "sha256=" + _sign(raw, secret)
    else:
        headers["X-Forgejo-Event"] = event
        headers["X-Forgejo-Delivery"] = delivery
        if sign:
            headers[signature_header or "X-Forgejo-Signature"] = _sign(raw, secret)
    params = {"project_id": project_id} if project_id else None
    return client.post(
        f"/v1/connectors/{forge}/webhook", content=raw, headers=headers, params=params
    )


def push_payload(branch: str = "dev/dev1/T-42", repo: str = "acme/partner", commits=None):
    return {
        "ref": f"refs/heads/{branch}",
        "before": "0" * 40,
        "after": "a" * 40,
        "repository": {"full_name": repo},
        "sender": {"login": "dev1"},
        "commits": commits
        if commits is not None
        else [
            {"id": "a" * 40, "timestamp": "2026-11-12T08:00:00Z",
             "message": "TOP SECRET CODE"},
            {"id": "b" * 40, "timestamp": "2026-11-12T08:05:00Z", "message": "еще"},
        ],
    }


def pr_payload(action: str = "closed", merged: bool = True, repo: str = "acme/partner"):
    return {
        "action": action,
        "number": 7,
        "repository": {"full_name": repo},
        "sender": {"login": "dev1"},
        "pull_request": {
            "number": 7,
            "state": "closed" if action == "closed" else "open",
            "merged": merged,
            "merged_at": "2026-11-12T12:00:00Z" if merged else None,
            "created_at": "2026-11-12T09:00:00Z",
            "updated_at": "2026-11-12T12:00:00Z",
            "additions": 120,
            "deletions": 30,
            "changed_files": 4,
            "head": {"ref": "dev/dev1/T-7"},
            "base": {"ref": "main"},
            "user": {"login": "dev1"},
            "body": "TOP SECRET PR BODY",
        },
    }


def issue_payload(labels: list[str], repo: str = "acme/partner"):
    return {
        "action": "opened",
        "repository": {"full_name": repo},
        "sender": {"login": "dev1"},
        "issue": {
            "number": 5,
            "state": "open",
            "labels": [{"name": label} for label in labels],
            "user": {"login": "dev1"},
            "created_at": "2026-11-12T09:00:00Z",
            "updated_at": "2026-11-12T09:30:00Z",
            "body": "TOP SECRET ISSUE BODY",
        },
    }


def _create_session(client, session_id: str, branch: str, task_ref: str | None = None):
    response = client.post(
        "/v1/sessions",
        json={
            "session_id": session_id, "participant_id": "dev1", "project_id": "partner",
            "task_ref": task_ref, "branch": branch,
            "started_at_utc": moscow_local(0),
        },
    )
    assert response.status_code == 201, response.text


def _heartbeat(client, event_id: str = "n1", task: str = "active_human",
               start_h: int = 10, end_h: int = 14, signals: dict | None = None):
    response = client.post(
        "/v1/heartbeats",
        json={
            "event_id": event_id, "session_id": "s1", "seq": 0, "cls": task,
            "start_utc": moscow_local(start_h), "end_utc": moscow_local(end_h),
            "received_at_utc": moscow_local(end_h),
            "signals": signals,
        },
    )
    assert response.status_code == 200, response.text


# ---------- подпись и периметр ----------


def test_webhook_without_secret_is_503(client):
    object.__setattr__(settings, "connector_secret", "")
    response = post_webhook(client, payload=push_payload())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "connector_not_configured"


def test_webhook_signature_missing_and_invalid(client, factory):
    missing = post_webhook(client, payload=push_payload(), sign=False)
    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "webhook_signature_missing"

    invalid = post_webhook(client, payload=push_payload(), secret="wrong-secret")
    assert invalid.status_code == 403
    assert invalid.json()["detail"]["code"] == "webhook_signature_invalid"
    with factory() as db:
        actions = [row.action for row in db.query(models.AuditLog).all()]
        assert "incident.webhook_signature_invalid" in actions


def test_unknown_repo_is_404(client):
    response = post_webhook(
        client, payload=push_payload(repo="other/repo"), delivery="unknown-1"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "repo_not_mapped"


# ---------- факты и привязка сессий ----------


def test_push_creates_facts_links_session_and_deduplicates(client, factory):
    _create_session(client, "s-link", "dev/dev1/T-42", task_ref=None)
    response = post_webhook(client, payload=push_payload(), delivery="push-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["facts"] == 3  # два commit + push
    assert body["linked_sessions"] == [
        {"session_id": "s-link", "branch": "dev/dev1/T-42", "task_ref": "T-42"}
    ]

    facts = client.get(
        "/v1/connectors/facts",
        params={"project_id": "partner", "branch": "dev/dev1/T-42"},
    ).json()
    assert facts["count"] == 3
    assert {fact["kind"] for fact in facts["facts"]} == {"commit", "push"}
    assert all(fact["task_ref"] == "T-42" for fact in facts["facts"])

    duplicate = post_webhook(client, payload=push_payload(), delivery="push-1")
    assert duplicate.json() == {"status": "duplicate", "delivery_id": "push-1"}
    facts_again = client.get(
        "/v1/connectors/facts", params={"project_id": "partner"}
    ).json()
    assert facts_again["count"] == 3
    with factory() as db:
        session = db.get(models.Session, "s-link")
        assert session.task_ref == "T-42"


def test_merged_pull_request_creates_acceptance_draft(client):
    _create_session(client, "s-pr", "dev/dev1/T-7", task_ref=None)
    response = post_webhook(
        client, event="pull_request", payload=pr_payload(), delivery="pr-1"
    )
    assert response.status_code == 200, response.text
    assert response.json()["facts"] == 2
    acceptance = client.get(
        "/v1/connectors/facts",
        params={"project_id": "partner", "kind": "acceptance_draft"},
    ).json()
    assert acceptance["count"] == 1
    assert acceptance["facts"][0]["task_ref"] == "T-7"


def test_issue_labels_create_task_acceptance_and_dispute_facts(client):
    response = post_webhook(
        client,
        event="issues",
        payload=issue_payload(["paid-task", "accepted", "dispute"]),
        delivery="issue-1",
    )
    assert response.status_code == 200, response.text
    assert response.json()["facts"] == 4
    task_facts = client.get(
        "/v1/connectors/facts", params={"project_id": "partner", "kind": "task"}
    ).json()
    assert task_facts["count"] == 1
    assert task_facts["facts"][0]["task_ref"] == "#5"


def test_forgejo_signature_and_gitea_variant(client, factory):
    with factory() as db:
        db.add(
            models.Project(
                project_id="forgejo-proj", tenant_id="fonii", name="Forgejo",
                timezone="Europe/Moscow", forge="forgejo", repo="acme/forge",
            )
        )
        db.commit()
    response = post_webhook(
        client,
        forge="forgejo",
        payload=push_payload(branch="dev/dev1/T-3", repo="acme/forge"),
        delivery="forgejo-1",
        signature_header="X-Forgejo-Signature",
    )
    assert response.status_code == 200, response.text
    assert response.json()["facts"] == 3

    raw = json.dumps(push_payload(repo="acme/forge")).encode()
    from partner_server.connectors import verify_signature

    ok, _ = verify_signature(
        "gitea", raw, {"X-Gitea-Signature": _sign(raw)}, SECRET
    )
    assert ok


def test_import_endpoint_is_idempotent(client):
    payload = {
        "project_id": "partner",
        "repo": "acme/partner",
        "forge": "github",
        "items": [
            {"kind": "branch", "ref": "dev/dev1/T-9", "branch": "dev/dev1/T-9",
             "task_ref": "T-9", "payload": {"commit": "c" * 40}},
            {"kind": "commit", "ref": "c" * 40, "branch": "dev/dev1/T-9",
             "task_ref": "T-9", "occurred_at": "2026-11-12T09:00:00Z"},
        ],
    }
    first = client.post("/v1/connectors/github/import", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["facts"] == 2
    second = client.post("/v1/connectors/github/import", json=payload)
    assert second.json()["status"] == "duplicate"


def test_raw_payload_does_not_store_code_or_bodies(client, factory):
    post_webhook(client, payload=push_payload(), delivery="privacy-push")
    post_webhook(client, event="pull_request", payload=pr_payload(), delivery="privacy-pr")
    with factory() as db:
        blob = json.dumps(
            [row.payload_json for row in db.query(models.ConnectorEvent).all()]
            + [row.payload_json for row in db.query(models.RepoFact).all()],
            ensure_ascii=False,
        )
    assert "TOP SECRET" not in blob
    assert "body" not in blob


# ---------- no_output ----------


def test_no_output_flag_and_clearing_by_commit_fact(client):
    _heartbeat(
        client,
        signals={"input_events": 10, "focus": 1, "tool_calls": 0},
    )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["paid_seconds"] > 3 * 3600
    assert "no_output" in day["flags"]

    post_webhook(
        client,
        payload=push_payload(
            branch="dev/dev1/main",
            commits=[{"id": "c" * 40, "timestamp": "2026-11-12T08:00:00Z"}],
        ),
        delivery="output-1",
    )
    day_after = client.get(
        "/v1/days/2026-11-12", params={"participant_id": "dev1"}
    ).json()
    assert "no_output" not in day_after["flags"]


def test_no_output_not_flagged_with_tool_calls(client):
    _heartbeat(
        client,
        signals={"input_events": 10, "focus": 1, "tool_calls": 3},
    )
    day = client.get("/v1/days/2026-11-12", params={"participant_id": "dev1"}).json()
    assert day["paid_seconds"] > 3 * 3600
    assert "no_output" not in day["flags"]
