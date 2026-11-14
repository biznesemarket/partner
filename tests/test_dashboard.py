"""Веб-дашборд директора (issue #10): страница и данные флагов.

Сценарии:
- D-10  /dashboard отдает самодостаточную страницу без данных и токена
- D-11  /v1/flags возвращает флаги месяца с исходной/эффективной суммой
- D-12  фильтры code/participant/month и limit
- D-13  /v1/flags закрыт Bearer-токеном, если он задан
"""
from __future__ import annotations

from partner_server import models
from partner_server.config import settings

DATE = "2026-11-12"
DAY_ID = "partner|dev1|2026-11-12"


def _day(day_id: str = DAY_ID) -> models.DayRecord:
    return models.DayRecord(
        day_id=day_id,
        participant_id="dev1",
        project_id="partner",
        date=DATE,
        tz="Europe/Moscow",
        paid_seconds=4 * 3600,
        amount_cents=200_000,
        cash_cents=0,
        equity_cents=200_000,
        share_bp="2.0000",
        rate_version=1,
        valuation_version=1,
        policy_version=1,
        sha256_input="a" * 64,
        closed=1,
        closed_at="2026-11-13T00:00:00+00:00",
    )


def _adjustment(delta: int = -50_000) -> models.Adjustment:
    return models.Adjustment(
        adjustment_id="adj-dashboard",
        day_id=DAY_ID,
        delta_cents=delta,
        reason="спор дня",
        approved_by="director",
        created_at="2026-11-13T10:00:00+00:00",
    )


def _seed_flag(factory, *, code: str = "no_output", detail: str = "4 ч без результата") -> None:
    with factory() as s:
        s.add(_day())
        s.add(
            models.Flag(
                day_id=DAY_ID,
                code=code,
                detail=detail,
            )
        )
        s.add(_adjustment())
        s.commit()


# --- D-10 ----------------------------------------------------------------

def test_dashboard_page_is_self_contained(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "Кабинет директора" in page
    assert "/v1/status/summary" in page and "/v1/flags" in page
    assert "/v1/disputes" in page and "/v1/snapshots" in page
    assert "sessionStorage" in page
    # страница не содержит данных; они приходят из API
    assert "no_output" not in page


# --- D-11 ----------------------------------------------------------------

def test_flags_returns_month_flags_with_effective_amount(client, factory):
    _seed_flag(factory)
    r = client.get("/v1/flags", params={"month": "2026-11"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["month"] == "2026-11"
    assert data["project_id"] == "partner"
    assert data["count"] == 1
    flag = data["flags"][0]
    assert flag["day_id"] == DAY_ID
    assert flag["participant_id"] == "dev1"
    assert flag["date"] == DATE
    assert flag["code"] == "no_output"
    assert flag["detail"] == "4 ч без результата"
    assert flag["paid_seconds"] == 4 * 3600
    assert flag["amount_cents"] == 200_000
    assert flag["effective_amount_cents"] == 150_000
    assert flag["closed"] is True


# --- D-12 ----------------------------------------------------------------

def test_flags_filters(client, factory):
    _seed_flag(factory)
    assert client.get("/v1/flags", params={"code": "no_output"}).json()["count"] == 1
    assert client.get("/v1/flags", params={"code": "clock_skew"}).json()["count"] == 0
    assert (
        client.get("/v1/flags", params={"participant_id": "dev1"}).json()["count"] == 1
    )
    assert (
        client.get("/v1/flags", params={"participant_id": "dev2"}).json()["count"] == 0
    )
    assert client.get("/v1/flags", params={"month": "2026-10"}).json()["count"] == 0
    assert client.get("/v1/flags", params={"limit": 0}).status_code == 422


# --- D-13 ----------------------------------------------------------------

def test_flags_require_bearer_when_configured(client, factory):
    _seed_flag(factory)
    old = settings.api_token
    object.__setattr__(settings, "api_token", "director-token")
    try:
        assert client.get("/v1/flags").status_code == 401
        r = client.get(
            "/v1/flags", headers={"Authorization": "Bearer director-token"}
        )
        assert r.status_code == 200 and r.json()["count"] == 1
    finally:
        object.__setattr__(settings, "api_token", old)

def test_summary_includes_append_only_adjustments(client, factory):
    """Сводка дашборда: исходная сумма и эффективная с корректировками."""
    _seed_flag(factory)
    r = client.get("/v1/status/summary", params={"month": "2026-11"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["project_id"] == "partner"
    row = data["participants"][0]
    assert row["participant_id"] == "dev1"
    assert row["paid_hours_month"] == "4"
    assert row["amount_rub_month"] == "2000"
    assert row["adjustments_delta_cents"] == -50_000
    assert row["effective_amount_rub"] == "1500"


def test_disputes_filter_by_month(client, factory):
    """Дашборд показывает споры выбранного месяца, а не все подряд."""
    with factory() as s:
        s.add(_day())
        s.add(
            models.Dispute(
                dispute_id="dsp-month",
                day_id=DAY_ID,
                participant_id="dev1",
                project_id="partner",
                status="open",
                opened_by="dev1",
                opened_at="2026-11-12T10:00:00+00:00",
                window_deadline="2026-11-15T10:00:00+00:00",
                interval_refs=[],
                reason="проверка месяца",
            )
        )
        s.commit()
    assert (
        client.get("/v1/disputes", params={"month": "2026-11"}).json()["count"] == 1
    )
    assert (
        client.get("/v1/disputes", params={"month": "2026-10"}).json()["count"] == 0
    )

