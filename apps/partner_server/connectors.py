"""Коннекторы git-платформ: нормализация вебхуков в факты репозитория.

Граница (connectors.md §1): коннектор не начисляет деньги. Он проверяет
подпись вебхука, сохраняет метаданные события и факты (ветки, коммиты,
PR, issues, приемка), привязывает сессии к задаче/ветке и дает данным
для флага no_output. Код, тела PR/issues и диффы не сохраняются.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from partner_cli.branching import task_key, task_ref_from_branch

from . import audit
from . import models
from .config import settings

FORGES = {"github", "forgejo", "gitea"}
TASK_LABEL = "paid-task"
ACCEPT_LABEL = "accepted"
DISPUTE_LABEL = "dispute"


class ConnectorError(Exception):
    """Ожидаемая ошибка коннектора → HTTP 4xx с кодом домена."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(f"{code}: {message}")


@dataclass
class FactDraft:
    kind: str
    ref: str | None = None
    task_ref: str | None = None
    branch: str | None = None
    actor: str | None = None
    occurred_at: str | None = None
    payload: dict = field(default_factory=dict)


# ---------- утилиты ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_ts(value: object) -> str | None:
    """ISO-время платформы → канонический UTC (+00:00), иначе None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def extract_repo(payload: dict) -> str:
    repo = payload.get("repository") or {}
    if isinstance(repo, dict):
        full_name = repo.get("full_name")
        if full_name:
            return str(full_name)
        owner = repo.get("owner") or {}
        name = repo.get("name")
        if isinstance(owner, dict):
            login = owner.get("login") or owner.get("username")
            if login and name:
                return f"{login}/{name}"
        if name:
            return str(name)
    return ""


def _sender(payload: dict) -> str | None:
    sender = payload.get("sender") or {}
    if isinstance(sender, dict):
        login = sender.get("login") or sender.get("username")
        if login:
            return str(login)
    if payload.get("pusher"):
        pusher = payload["pusher"]
        if isinstance(pusher, dict):
            return str(pusher.get("name") or pusher.get("email") or "") or None
        return str(pusher)
    return None


def _branch_name(ref: object) -> str | None:
    if not isinstance(ref, str) or not ref:
        return None
    return ref.removeprefix("refs/heads/") or None


# ---------- нормализация событий ----------


def extract_facts(
    event_type: str, payload: dict, *, default_branch: str = "main"
) -> list[FactDraft]:
    """Матрица «событие → факты» (connectors.md §4)."""
    event = (event_type or "").lower()
    if event == "push":
        return _push_facts(payload, default_branch)
    if event in ("pull_request", "pull-request", "pull_request_review"):
        return _pull_request_facts(payload, default_branch)
    if event in ("issues", "issue"):
        return _issue_facts(payload)
    return []


def _push_facts(payload: dict, default_branch: str) -> list[FactDraft]:
    branch = _branch_name(payload.get("ref"))
    if not branch:
        return []
    task_ref = task_ref_from_branch(branch, default_branch)
    actor = _sender(payload)
    commits = payload.get("commits") or []
    commit_ids: list[str] = []
    facts: list[FactDraft] = []

    for commit in commits:
        if not isinstance(commit, dict):
            continue
        sha = commit.get("id") or commit.get("sha")
        if not sha:
            continue
        commit_ids.append(str(sha))
        facts.append(
            FactDraft(
                kind="commit",
                ref=str(sha),
                task_ref=task_ref,
                branch=branch,
                actor=actor,
                occurred_at=normalize_ts(commit.get("timestamp")),
                payload={"branch": branch, "distinct": bool(commit.get("distinct", True))},
            )
        )

    occurred = None
    if facts:
        occurred = facts[-1].occurred_at
    facts.append(
        FactDraft(
            kind="push",
            ref=branch,
            task_ref=task_ref,
            branch=branch,
            actor=actor,
            occurred_at=occurred or _now_iso(),
            payload={
                "before": payload.get("before"),
                "after": payload.get("after"),
                "created": bool(payload.get("created", False)),
                "deleted": bool(payload.get("deleted", False)),
                "forced": bool(payload.get("forced", False)),
                "commit_count": len(commit_ids),
                "commit_ids": commit_ids[:100],
            },
        )
    )
    if payload.get("created"):
        facts.append(FactDraft(kind="branch_created", ref=branch, branch=branch,
                               task_ref=task_ref, actor=actor, occurred_at=_now_iso()))
    if payload.get("deleted"):
        facts.append(FactDraft(kind="branch_deleted", ref=branch, branch=branch,
                               task_ref=task_ref, actor=actor, occurred_at=_now_iso()))
    return facts


def _pull_request_facts(payload: dict, default_branch: str) -> list[FactDraft]:
    pr = payload.get("pull_request") or {}
    if not isinstance(pr, dict):
        return []
    number = pr.get("number") or payload.get("number")
    head = (pr.get("head") or {}).get("ref")
    base = (pr.get("base") or {}).get("ref")
    branch = str(head) if head else None
    action = payload.get("action")
    merged = bool(pr.get("merged")) or bool(pr.get("merged_at"))
    user = pr.get("user") or {}
    actor = _sender(payload) or (
        str(user.get("login")) if isinstance(user, dict) and user.get("login") else None
    )
    task_ref = task_ref_from_branch(branch, default_branch)
    ref = f"#{number}" if number is not None else None
    facts = [
        FactDraft(
            kind="pull_request",
            ref=ref,
            task_ref=task_ref,
            branch=branch,
            actor=actor,
            occurred_at=normalize_ts(
                pr.get("merged_at") or pr.get("updated_at") or pr.get("created_at")
            ),
            payload={
                "action": action,
                "number": number,
                "state": pr.get("state"),
                "merged": merged,
                "draft": bool(pr.get("draft", False)),
                "base": base,
                "head": head,
                "additions": int(pr.get("additions") or 0),
                "deletions": int(pr.get("deletions") or 0),
                "changed_files": int(pr.get("changed_files") or 0),
            },
        )
    ]
    if merged:
        facts.append(
            FactDraft(
                kind="acceptance_draft",
                ref=f"pr-{number}" if number is not None else ref,
                task_ref=task_ref,
                branch=branch,
                actor=actor,
                occurred_at=normalize_ts(pr.get("merged_at")) or _now_iso(),
                payload={"number": number, "merged": True},
            )
        )
    return facts


def _issue_facts(payload: dict) -> list[FactDraft]:
    issue = payload.get("issue") or {}
    if not isinstance(issue, dict):
        return []
    number = issue.get("number") or payload.get("number")
    labels = issue.get("labels") or []
    label_names = {
        str(label.get("name")) for label in labels if isinstance(label, dict) and label.get("name")
    }
    user = issue.get("user") or {}
    actor = _sender(payload) or (
        str(user.get("login")) if isinstance(user, dict) and user.get("login") else None
    )
    ref = f"#{number}" if number is not None else None
    task_ref = ref
    occurred = normalize_ts(issue.get("updated_at") or issue.get("created_at"))
    facts = [
        FactDraft(
            kind="issue",
            ref=ref,
            task_ref=task_ref,
            actor=actor,
            occurred_at=occurred,
            payload={
                "action": payload.get("action"),
                "number": number,
                "state": issue.get("state"),
                "labels": sorted(label_names),
            },
        )
    ]
    if TASK_LABEL in label_names:
        facts.append(FactDraft(kind="task", ref=ref, task_ref=task_ref, actor=actor,
                               occurred_at=occurred, payload={"number": number}))
    if ACCEPT_LABEL in label_names:
        facts.append(FactDraft(kind="acceptance", ref=ref, task_ref=task_ref, actor=actor,
                               occurred_at=occurred, payload={"number": number}))
    if DISPUTE_LABEL in label_names:
        facts.append(FactDraft(kind="dispute_link", ref=ref, task_ref=task_ref,
                               actor=actor, occurred_at=occurred,
                               payload={"number": number}))
    return facts


# ---------- проверка подписи ----------


def verify_signature(
    forge: str, raw: bytes, headers: dict[str, str], secret: str
) -> tuple[bool, str]:
    """Проверить подпись вебхука; вернуть (ok, причина)."""
    lower = {key.lower(): value for key, value in headers.items()}
    expected_sha256 = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if forge == "github":
        signature = lower.get("x-hub-signature-256")
        if signature:
            provided = signature.removeprefix("sha256=").strip()
            return hmac.compare_digest(provided, expected_sha256), "invalid"
        legacy = lower.get("x-hub-signature")
        if legacy:
            expected_sha1 = hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()
            provided = legacy.removeprefix("sha1=").strip()
            return hmac.compare_digest(provided, expected_sha1), "invalid"
        return False, "missing"
    signature = (
        lower.get("x-forgejo-signature")
        or lower.get("x-gitea-signature")
        or lower.get("x-hub-signature-256")
        or ""
    )
    if not signature:
        return False, "missing"
    provided = signature.removeprefix("sha256=").strip()
    return hmac.compare_digest(provided, expected_sha256), "invalid"


def event_type_from_headers(headers: dict[str, str], payload: dict) -> str:
    lower = {key.lower(): value for key, value in headers.items()}
    for name in ("x-github-event", "x-forgejo-event", "x-gitea-event", "x-event"):
        if lower.get(name):
            return lower[name]
    if "commits" in payload or "ref" in payload:
        return "push"
    if "pull_request" in payload:
        return "pull_request"
    if "issue" in payload:
        return "issues"
    return "unknown"


def delivery_from_headers(headers: dict[str, str], raw: bytes) -> str:
    lower = {key.lower(): value for key, value in headers.items()}
    for name in ("x-github-delivery", "x-forgejo-delivery", "x-gitea-delivery"):
        if lower.get(name):
            return lower[name]
    return hashlib.sha256(raw).hexdigest()[:64]


# ---------- БД ----------


def webhook_secret_for(project_id: str) -> str:
    """Секрет проекта: PARTNER_CONNECTOR_SECRETS (JSON) или общий env."""
    raw = settings.connector_secrets_json
    if raw:
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError:
            mapping = {}
        if isinstance(mapping, dict) and mapping.get(project_id):
            return str(mapping[project_id])
    return settings.connector_secret


def project_for_repo(
    db: Session, forge: str, repo: str, project_id: str | None = None
) -> models.Project:
    if forge not in FORGES:
        raise ConnectorError("unknown_forge", f"платформа {forge!r} не поддерживается", 404)
    if project_id:
        project = db.get(models.Project, project_id)
        if project is None:
            raise ConnectorError("project_not_found", f"проект {project_id!r} не найден", 404)
        if project.repo and project.repo.lower() != (repo or "").lower():
            raise ConnectorError(
                "repo_project_mismatch",
                f"repo {repo!r} не совпадает с проектом {project_id!r}",
                403,
            )
        if (project.forge or "github") != forge:
            raise ConnectorError(
                "forge_project_mismatch",
                f"проект {project_id!r} привязан к {project.forge!r}, а не {forge!r}",
                403,
            )
        return project
    if not repo:
        raise ConnectorError("repo_not_mapped", "в вебхуке нет repository.full_name", 404)
    rows = db.scalars(select(models.Project)).all()
    matches = [
        p
        for p in rows
        if (p.repo or "").lower() == repo.lower() and (p.forge or "github") == forge
    ]
    if not matches:
        raise ConnectorError("repo_not_mapped", f"репозиторий {repo!r} не привязан к проекту", 404)
    if len(matches) > 1:
        raise ConnectorError("repo_ambiguous", f"репозиторий {repo!r} привязан к нескольким проектам", 409)
    return matches[0]


def link_sessions(
    db: Session, project: models.Project, facts: list[FactDraft]
) -> list[dict]:
    """Заполнить пустые task_ref/branch сессий найденными фактами."""
    linked: list[dict] = []
    for fact in facts:
        if not fact.branch and not fact.task_ref:
            continue
        sessions = db.scalars(
            select(models.Session).where(models.Session.project_id == project.project_id)
        ).all()
        for session in sessions:
            changed = False
            if fact.branch and session.branch == fact.branch and not session.task_ref and fact.task_ref:
                session.task_ref = fact.task_ref
                changed = True
            if (
                fact.task_ref
                and task_key(session.task_ref) == task_key(fact.task_ref)
                and fact.branch
                and not session.branch
            ):
                session.branch = fact.branch
                changed = True
            if changed:
                linked.append(
                    {"session_id": session.session_id, "branch": session.branch,
                     "task_ref": session.task_ref}
                )
    return linked


def ingest_event(
    db: Session,
    *,
    project: models.Project,
    forge: str,
    delivery_id: str,
    event_type: str,
    payload: dict,
    source: str = "webhook",
    facts: list[FactDraft] | None = None,
) -> dict:
    """Сохранить событие идемпотентно и материализовать факты."""
    if db.get(models.ConnectorEvent, delivery_id) is not None:
        return {"status": "duplicate", "delivery_id": delivery_id}
    default_branch = project.default_branch or "main"
    if facts is None:
        facts = extract_facts(event_type, payload, default_branch=default_branch)
    now = _now_iso()
    db.add(
        models.ConnectorEvent(
            delivery_id=delivery_id,
            forge=forge,
            repo=extract_repo(payload) or (project.repo or ""),
            event_type=event_type,
            action=str(payload.get("action") or "") or None,
            project_id=project.project_id,
            payload_json={
                "action": payload.get("action"),
                "repository": extract_repo(payload),
                "facts": [fact.kind for fact in facts],
            },
            received_at=now,
            source=source,
        )
    )
    created = 0
    for fact in facts:
        fact_id = "fact-" + hashlib.sha256(
            f"{delivery_id}|{fact.kind}|{fact.ref}|{fact.branch}".encode()
        ).hexdigest()[:32]
        if db.get(models.RepoFact, fact_id) is not None:
            continue
        db.add(
            models.RepoFact(
                fact_id=fact_id,
                project_id=project.project_id,
                delivery_id=delivery_id,
                kind=fact.kind,
                ref=fact.ref,
                task_ref=fact.task_ref,
                branch=fact.branch,
                actor=fact.actor,
                payload_json=fact.payload,
                occurred_at=fact.occurred_at or now,
                created_at=now,
            )
        )
        created += 1
    linked = link_sessions(db, project, facts)
    db.commit()
    audit.record(
        db,
        actor=f"connector:{forge}",
        entity="connector_events",
        entity_id=delivery_id,
        action="event.received",
        new_value={
            "project": project.project_id,
            "type": event_type,
            "facts": created,
            "linked_sessions": len(linked),
        },
    )
    return {
        "status": "accepted",
        "delivery_id": delivery_id,
        "facts": created,
        "linked_sessions": linked,
    }


def facts_for_window(
    db: Session,
    project_id: str,
    lower_iso: str,
    upper_iso: str,
    *,
    kinds: tuple[str, ...] = ("commit", "pull_request", "push"),
) -> list[models.RepoFact]:
    rows = db.scalars(
        select(models.RepoFact)
        .where(models.RepoFact.project_id == project_id)
        .where(models.RepoFact.kind.in_(kinds))
        .where(models.RepoFact.occurred_at >= lower_iso)
        .where(models.RepoFact.occurred_at < upper_iso)
    ).all()
    return list(rows)


def output_activity(
    db: Session,
    project_id: str,
    lower_iso: str,
    upper_iso: str,
    branches: set[str],
    task_refs: set[str],
) -> dict:
    """Сводка «есть результат» для no_output: коммиты, PR, диффы."""
    facts = db.scalars(
        select(models.RepoFact)
        .where(models.RepoFact.project_id == project_id)
        .where(models.RepoFact.kind.in_(("commit", "pull_request")))
        .where(models.RepoFact.occurred_at >= lower_iso)
        .where(models.RepoFact.occurred_at < upper_iso)
    ).all()
    commits = 0
    pull_requests = 0
    diff_lines = 0
    for fact in facts:
        matches = (fact.branch and fact.branch in branches) or (
            fact.task_ref and task_key(fact.task_ref) in task_refs
        )
        if not matches:
            continue
        if fact.kind == "commit":
            commits += 1
        elif fact.kind == "pull_request":
            pull_requests += 1
            payload = fact.payload_json or {}
            diff_lines += int(payload.get("additions") or 0) + int(
                payload.get("deletions") or 0
            )
    return {
        "commits": commits,
        "pull_requests": pull_requests,
        "diff_lines": diff_lines,
        "has_output": bool(commits or pull_requests or diff_lines),
    }
