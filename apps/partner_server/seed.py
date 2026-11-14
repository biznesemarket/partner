"""Идемпотентный seed демо-tenant для smoke-тестов и CI (issue #5).

Создает tenant/project/participant/policy/rate/session. Используется
PostgreSQL-smoke в CI и локальной проверкой:

    PARTNER_DB_URL=postgresql+psycopg://... python -m partner_server.seed
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from . import models
from .config import settings
from .db import Base, make_engine, make_sessionmaker

DEFAULT_POLICY = {
    "rate_rub_per_hour": 500,
    "valuation_rub": 10_000_000,
    "daily_cap_hours": 12.0,
    "autonomous_paid_cap_hours": 2.0,
    "k_supervised": 1.0,
    "k_autonomous": 0.2,
}


def seed(
    sess,
    *,
    tenant_id: str = "fonii",
    project_id: str = "partner",
    participant_id: str = "dev1",
    session_id: str = "s1",
    policy_id: str = "p1",
) -> dict:
    """Создать недостающие записи; повторный вызов ничего не дублирует."""
    if sess.get(models.Tenant, tenant_id) is None:
        sess.add(models.Tenant(tenant_id=tenant_id, name=f"Tenant {tenant_id}"))
    if sess.get(models.Project, project_id) is None:
        sess.add(
            models.Project(
                project_id=project_id,
                tenant_id=tenant_id,
                name=f"Project {project_id}",
                timezone="Europe/Moscow",
                forge="github",
                repo="acme/partner",
            )
        )
    if sess.get(models.Participant, participant_id) is None:
        sess.add(
            models.Participant(
                participant_id=participant_id,
                tenant_id=tenant_id,
                display_name=f"Participant {participant_id}",
            )
        )
    if sess.get(models.Policy, policy_id) is None:
        sess.add(
            models.Policy(
                policy_id=policy_id,
                project_id=project_id,
                policy_version=1,
                sha256="0" * 64,
                created_at="2026-11-01T00:00:00+00:00",
                params_json=dict(DEFAULT_POLICY),
            )
        )
    if sess.get(models.RateVersion, (policy_id, "2026-10-01")) is None:
        sess.add(
            models.RateVersion(
                policy_id=policy_id,
                effective_from="2026-10-01",
                value_cents=50_000,
                reason="seed",
                approved_by="director",
            )
        )
    if sess.get(models.Session, session_id) is None:
        sess.add(
            models.Session(
                session_id=session_id,
                participant_id=participant_id,
                project_id=project_id,
                task_ref="T-1",
                branch=f"dev/{participant_id}/t1",
                started_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        )
    sess.commit()
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "participant_id": participant_id,
        "session_id": session_id,
        "policy_id": policy_id,
    }


def main(argv: list[str] | None = None, db_url: str | None = None) -> int:
    parser = argparse.ArgumentParser(description="Идемпотентный seed демо-tenant")
    parser.add_argument("--tenant", default="fonii")
    parser.add_argument("--project", default="partner")
    parser.add_argument("--participant", default="dev1")
    parser.add_argument("--session", default="s1")
    args = parser.parse_args(argv)

    engine = make_engine(db_url or settings.db_url)
    if (db_url or settings.db_url).startswith("sqlite"):
        Base.metadata.create_all(engine)
    with make_sessionmaker(engine)() as sess:
        result = seed(
            sess,
            tenant_id=args.tenant,
            project_id=args.project,
            participant_id=args.participant,
            session_id=args.session,
        )
    print("seed:", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
