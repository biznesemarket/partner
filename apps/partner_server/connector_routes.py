"""HTTP-периметр коннекторов: вебхуки, импорт фактов, чтение фактов.

Вебхуки аутентифицируются HMAC-подписью платформы (connectors.md §5),
остальные эндпоинты — обычным Bearer-токеном сервера.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, connectors, models
from .db import get_db
from .routes import get_project, require_token

router = APIRouter(prefix="/v1/connectors", tags=["connectors"])

FACT_KINDS = (
    "branch", "branch_created", "branch_deleted", "commit", "push",
    "pull_request", "issue", "task", "acceptance", "acceptance_draft",
    "dispute_link",
)


class ImportItemIn(BaseModel):
    kind: str = Field(pattern="^(" + "|".join(FACT_KINDS) + ")$")
    ref: str | None = None
    task_ref: str | None = None
    branch: str | None = None
    actor: str | None = None
    occurred_at: str | None = None
    payload: dict = Field(default_factory=dict)


class ImportIn(BaseModel):
    project_id: str
    repo: str
    forge: str = "github"
    source: str = "api"
    delivery_id: str | None = None
    items: list[ImportItemIn] = Field(default_factory=list, min_length=1)


def _raise(err: connectors.ConnectorError) -> None:
    raise HTTPException(err.status, {"code": err.code, "detail": err.message})


@router.post("/{forge}/webhook")
async def webhook(
    forge: str,
    request: Request,
    project_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Прием вебхука GitHub/Forgejo/Gitea с проверкой HMAC-подписи."""
    raw = await request.body()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise HTTPException(400, {"code": "invalid_json", "detail": "тело не JSON"})
    if not isinstance(payload, dict):
        raise HTTPException(400, {"code": "invalid_json", "detail": "ожидается JSON-объект"})
    headers = {key: value for key, value in request.headers.items()}
    delivery_id = connectors.delivery_from_headers(headers, raw)
    try:
        project = connectors.project_for_repo(
            db, forge, connectors.extract_repo(payload), project_id
        )
    except connectors.ConnectorError as err:
        _raise(err)
    secret = connectors.webhook_secret_for(project.project_id)
    if not secret:
        audit.incident(
            db, code="connector_secret_missing", entity="connector_events",
            entity_id=delivery_id, meta={"project": project.project_id, "forge": forge},
        )
        raise HTTPException(
            503,
            {"code": "connector_not_configured",
             "detail": "секрет вебхука не задан (PARTNER_CONNECTOR_SECRET)"},
        )
    ok, reason = connectors.verify_signature(forge, raw, headers, secret)
    if not ok:
        if reason == "missing":
            raise HTTPException(
                401, {"code": "webhook_signature_missing",
                      "detail": "подпись вебхука обязательна"}
            )
        audit.incident(
            db, code="webhook_signature_invalid", entity="connector_events",
            entity_id=delivery_id,
            meta={"project": project.project_id, "forge": forge},
        )
        raise HTTPException(
            403, {"code": "webhook_signature_invalid",
                  "detail": "HMAC-подпись не сходится с секретом проекта"}
        )
    event_type = connectors.event_type_from_headers(headers, payload)
    try:
        return connectors.ingest_event(
            db,
            project=project,
            forge=forge,
            delivery_id=delivery_id,
            event_type=event_type,
            payload=payload,
            source="webhook",
        )
    except connectors.ConnectorError as err:
        _raise(err)


@router.post("/{forge}/import")
def import_facts(
    forge: str,
    payload: ImportIn,
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Импорт фактов из API платформы (backfill) — идемпотентно по delivery_id."""
    if forge not in connectors.FORGES:
        raise HTTPException(404, {"code": "unknown_forge", "detail": forge})
    try:
        project = connectors.project_for_repo(db, forge, payload.repo, payload.project_id)
    except connectors.ConnectorError as err:
        _raise(err)
    facts = [
        connectors.FactDraft(
            kind=item.kind,
            ref=item.ref,
            task_ref=item.task_ref,
            branch=item.branch,
            actor=item.actor,
            occurred_at=connectors.normalize_ts(item.occurred_at),
            payload=item.payload,
        )
        for item in payload.items
    ]
    digest = hashlib.sha256(
        json.dumps(payload.model_dump(), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:32]
    delivery_id = payload.delivery_id or f"import-{digest}"
    try:
        return connectors.ingest_event(
            db,
            project=project,
            forge=forge,
            delivery_id=delivery_id,
            event_type="import",
            payload={"repository": {"full_name": payload.repo}, "action": "import"},
            source=payload.source,
            facts=facts,
        )
    except connectors.ConnectorError as err:
        _raise(err)


@router.get("/facts")
def list_facts(
    kind: str | None = Query(default=None),
    task_ref: str | None = Query(default=None),
    branch: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO UTC, включительно"),
    until: str | None = Query(default=None, description="ISO UTC, исключительно"),
    limit: int = Query(default=100, ge=1, le=500),
    project: models.Project = Depends(get_project),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Факты репозитория проекта: ветки, коммиты, PR, issues, приемка."""
    stmt = (
        select(models.RepoFact)
        .where(models.RepoFact.project_id == project.project_id)
        .order_by(models.RepoFact.created_at.desc())
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(models.RepoFact.kind == kind)
    if task_ref:
        stmt = stmt.where(models.RepoFact.task_ref == task_ref)
    if branch:
        stmt = stmt.where(models.RepoFact.branch == branch)
    if since:
        stmt = stmt.where(models.RepoFact.occurred_at >= since)
    if until:
        stmt = stmt.where(models.RepoFact.occurred_at < until)
    rows = db.scalars(stmt).all()
    return {
        "count": len(rows),
        "facts": [
            {
                "fact_id": f.fact_id,
                "kind": f.kind,
                "ref": f.ref,
                "task_ref": f.task_ref,
                "branch": f.branch,
                "actor": f.actor,
                "occurred_at": f.occurred_at,
                "payload": f.payload_json,
            }
            for f in rows
        ],
    }


@router.get("/events")
def list_events(
    limit: int = Query(default=100, ge=1, le=500),
    project: models.Project = Depends(get_project),
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
):
    """Журнал принятых доставок (без сырых payload) — диагностика коннектора."""
    rows = db.scalars(
        select(models.ConnectorEvent)
        .where(models.ConnectorEvent.project_id == project.project_id)
        .order_by(models.ConnectorEvent.received_at.desc())
        .limit(limit)
    ).all()
    return {
        "count": len(rows),
        "events": [
            {
                "delivery_id": e.delivery_id,
                "forge": e.forge,
                "repo": e.repo,
                "event_type": e.event_type,
                "action": e.action,
                "source": e.source,
                "received_at": e.received_at,
            }
            for e in rows
        ],
    }
