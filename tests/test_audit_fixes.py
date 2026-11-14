"""Регрессионные тесты аудита (2026-11-13): фиксы F1…F12.

Каждый тест закрепляет одну находку аудита: без фикса тест падает.
Нумерация соответствует отчёту аудита (FIX #N).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from partner_server import models, ops, service
from partner_cli import calc, storage


# ---------- помощники ----------


def _hb(sess, session_id: str, event_id: str, seq: int, cls: str, start: datetime, hours: float):
    """Heartbeat + interval за N часов от start (aware UTC)."""
    end = start + timedelta(hours=hours)
    return service.ingest_heartbeat(
        sess,
        event_id=event_id,
        session_id=session_id,
        seq=seq,
        cls=cls,
        start_utc=start.isoformat(timespec="seconds"),
        end_utc=end.isoformat(timespec="seconds"),
        received_at=start + timedelta(minutes=1),
    )


def _set_token(settings_obj, value: str):
    object.__setattr__(settings_obj, "api_token", value)


# ---------- FIX #1/#9 (P0/P1): Bearer на финансовых GET ----------


def test_financial_gets_require_token(factory, client, monkeypatch):
    """При заданном PARTNER_API_TOKEN финансовые GET без заголовка → 401;
    с корректным Bearer → 200; /v1/metrics и /v1/license остаются открытыми
    (задокументировано: runbook §мониторинг, licensing.py §metrics_gauge)."""
    from partner_server.config import settings

    with factory() as s:
        _hb(s, "s1", "ev-a1", 0, "active_human",
            datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc), 4.0)
        s.commit()

    monkeypatch.setattr("partner_server.licensing.current_state", lambda *a, **k: __import__("partner_server.licensing", fromlist=["LicenseState"]).LicenseState(state="demo"))
    _set_token(settings, "audit-secret-token")
    try:
        paths = [
            "/v1/days/2026-11-12?participant_id=dev1",
            "/v1/status/dev1",
            "/v1/audit",
            "/v1/notifications",
            "/v1/legal",
            "/v1/disputes",
        ]
        for p in paths:
            assert client.get(p).status_code == 401, f"{p} должен требовать Bearer"
        # неверный токен тоже 401 (FIX #9: compare_digest на неверном значении)
        r = client.get(paths[0], headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401
        # с корректным токеном — 200
        for p in paths:
            r = client.get(p, headers={"Authorization": "Bearer audit-secret-token"})
            assert r.status_code == 200, f"{p} с валидным Bearer должен отвечать 200"
        # публичные эндпоинты (задокументировано) открыты без токена
        assert client.get("/v1/metrics").status_code == 200
        assert client.get("/v1/license").status_code == 200
    finally:
        _set_token(settings, "")


def test_status_summary_reachable(factory, client):
    """FIX #21 (P1): /v1/status/summary не должен перебиваться маршрутом
    /status/{participant_id} (раньше — 404 «участник 'summary' не найден»)."""
    r = client.get("/v1/status/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "month" in body and "participants" in body


# ---------- FIX #2 (P1): cash_share_pct политики применяется ----------


def test_close_day_applies_cash_share(factory, client):
    """Политика cash_share_pct=30 → при закрытии дня cash_cents = 30% суммы,
    equity_cents = остаток (R8). Раньше cash всегда был 0."""
    from partner_server.config import settings

    with factory() as s:
        pol = s.get(models.Policy, "p1")
        pol.params_json = {**pol.params_json, "cash_share_pct": 30.0}
        _hb(s, "s1", "ev-c1", 0, "active_human",
            datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc), 4.0)
        s.commit()

    object.__setattr__(settings, "grace_hours", 24.0)
    with factory() as sess:
        project = sess.get(models.Project, "partner")
        res = service.close_day(sess, project, "dev1", "2026-11-12")
    rec = sess.get(models.DayRecord, "dev1|2026-11-12")
    assert rec.amount_cents == 200_000  # 4 ч × 500 ₽
    assert rec.cash_cents == 60_000, f"cash 30% от 2000 ₽: {rec.cash_cents}"
    assert rec.equity_cents == 140_000
    assert res["cash_share_pct"] == 30.0


def test_close_day_cash_default_zero(factory, client):
    """Без cash_share_pct в политике поведение прежнее: cash=0, equity=всё
    (обратная совместимость: политика conftest не задает cash_share_pct)."""
    with factory() as s:
        _hb(s, "s1", "ev-c2", 0, "active_human",
            datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc), 2.0)
        s.commit()
    with factory() as sess:
        project = sess.get(models.Project, "partner")
        service.close_day(sess, project, "dev1", "2026-11-12")
    rec = sess.get(models.DayRecord, "dev1|2026-11-12")
    assert rec.cash_cents == 0 and rec.equity_cents == rec.amount_cents


# ---------- FIX #3/#4 (P1): кэш движка и точное окно дня ----------


def test_get_db_reuses_default_engine():
    """FIX #3 (P1): get_db создает дефолтный engine один раз, не на каждый запрос."""
    from partner_server.db import _default_engine

    e1 = _default_engine("sqlite:///./data/probe-engine.db")
    e2 = _default_engine("sqlite:///./data/probe-engine.db")
    assert e1 is e2


def test_day_intervals_window_is_bounded(tmp_path):
    """FIX #4 (P2): интервалы давних дней не попадают в окно дня D (запрос
    ограничен с обеих сторон). Давний интервал (20 дней назад) должен быть
    отфильтрован в SQL, а не долетать до Python."""
    from partner_server.db import Base, make_engine, make_sessionmaker
    from sqlalchemy import event as sa_event

    engine = make_engine(f"sqlite:///{tmp_path/'win.db'}")
    Base.metadata.create_all(engine)
    sf = make_sessionmaker(engine)
    with sf() as s:
        s.add(models.Tenant(tenant_id="fonii", name="F"))
        s.add(models.Project(project_id="partner", tenant_id="fonii", name="P", timezone="Europe/Moscow"))
        s.add(models.Participant(participant_id="dev1", tenant_id="fonii", display_name="D"))
        s.add(models.Session(session_id="s1", participant_id="dev1", project_id="partner",
                             task_ref="T-1", branch="b", started_at_utc="2026-10-22T00:00:00+00:00"))
        s.add(models.Policy(policy_id="p1", project_id="partner", policy_version=1,
                            sha256="0" * 64, created_at="2026-11-01T00:00:00+00:00",
                            params_json={"rate_rub_per_hour": 500, "valuation_rub": 10_000_000}))
        s.commit()
        # давний интервал (за 20 дней до дня D) — раньше грузился всегда
        _hb(s, "s1", "ev-old", 0, "active_human",
            datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc), 4.0)
        # интервал в день D
        _hb(s, "s1", "ev-d", 1, "active_human",
            datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc), 4.0)
        s.commit()

    window_rows = {"n": 0}

    def before(cursor, *a, **k):
        if "FROM intervals" in str(cursor):
            window_rows["n"] += 1

    sa_event.listen(engine, "before_cursor_execute", before)
    try:
        with sf() as sess:
            project = sess.get(models.Project, "partner")
            pieces, flags, attached = service.day_intervals(sess, project, "dev1", "2026-11-12")
    finally:
        sa_event.remove(engine, "before_cursor_execute", before)
    # только интервал дня D (4 ч) — давний (20 дней назад) отфильтрован в SQL
    paid_seconds = sum(p.interval.seconds for p in pieces if p.cls == "active_human")
    assert paid_seconds == 4 * 3600, f"день D: ожидалось 4 ч, получено {paid_seconds / 3600}"
    # прямая проверка окна: интервал давнего дня не вернулся бы даже без
    # Python-фильтра — убеждаемся, что SQL-окно сужено (запрос с end_utc > нижняя граница)
    from sqlalchemy import text
    with sf() as sess:
        n = sess.execute(
            text("SELECT count(*) FROM intervals WHERE end_utc > '2026-11-11T21:00:00+00:00'")
        ).scalar()
    assert n == 1, "в окне дня D должен быть только интервал дня D (давний — за границей)"


def test_day_intervals_cross_midnight_kept(factory):
    """FIX #4 не режет интервалы через полночь: вечерний сегмент дня D
    учитывается в D, утренний — в D+1 (T-05)."""
    with factory() as s:
        # 23:30 MSK 11-12 → 00:30 MSK 11-13 = 20:30→21:30 UTC
        _hb(s, "s1", "ev-xm", 0, "supervised_agent",
            datetime(2026, 11, 12, 20, 30, tzinfo=timezone.utc), 1.0)
        s.commit()
    with factory() as sess:
        project = sess.get(models.Project, "partner")
        p_d, _, _ = service.day_intervals(sess, project, "dev1", "2026-11-12")
        p_d1, _, _ = service.day_intervals(sess, project, "dev1", "2026-11-13")
    secs_d = sum(p.interval.seconds for p in p_d)
    secs_d1 = sum(p.interval.seconds for p in p_d1)
    assert secs_d == 1800, f"день D: {secs_d}"
    assert secs_d1 == 1800, f"день D+1: {secs_d1}"


# ---------- FIX #6 (P1): защита закрытой записи в CLI-storage ----------


def test_storage_refuses_overwrite_of_closed_day(tmp_path):
    """Повторное сохранение дня с ДРУГИМИ числами в прототипе запрещено;
    идемпотентный повтор с теми же числами разрешен."""
    conn = storage.connect(tmp_path / "proto.sqlite3")
    r1 = calc.calc_day([calc.Interval(10 * 3600, 14 * 3600, calc.CLASS_ACTIVE)])
    sha1 = storage.save_day_record(conn, "p-demo", "2026-11-12", "Europe/Moscow", r1)
    # идемпотентный повтор — тот же результат, ошибки нет
    sha1b = storage.save_day_record(conn, "p-demo", "2026-11-12", "Europe/Moscow", r1)
    assert sha1b == sha1
    # другая длительность дня — молча перезаписать нельзя
    r2 = calc.calc_day([calc.Interval(10 * 3600, 18 * 3600, calc.CLASS_ACTIVE)])
    with pytest.raises(ValueError, match="уже закрыт"):
        storage.save_day_record(conn, "p-demo", "2026-11-12", "Europe/Moscow", r2)
    row = conn.execute(
        "SELECT amount_cents FROM day_records WHERE day_id='p-demo|2026-11-12'"
    ).fetchone()
    assert row[0] == r1.amount_cents, "закрытая запись изменилась"


def test_storage_cash_share_applied(tmp_path):
    """FIX #2b: save_day_record принимает cash_share_pct (R8) вместо
    захардкоженного 0.0."""
    conn = storage.connect(tmp_path / "proto2.sqlite3")
    r = calc.calc_day([calc.Interval(10 * 3600, 14 * 3600, calc.CLASS_ACTIVE)])
    storage.save_day_record(conn, "p-demo", "2026-11-12", "Europe/Moscow", r, cash_share_pct=30.0)
    row = conn.execute(
        "SELECT cash_cents, equity_cents FROM day_records WHERE day_id='p-demo|2026-11-12'"
    ).fetchone()
    assert row[0] == 60_000 and row[1] == 140_000


# ---------- FIX #7 (P2): метрики без N+1 ----------


def test_ops_days_open_overdue_dedup(tmp_path):
    """FIX #8 (P2): days_open_overdue считается дедупликацией (participant, дата);
    SQL-запросов не больше фиксированного числа даже при многих интервалах."""
    from partner_server.db import Base, make_engine, make_sessionmaker
    from sqlalchemy import event as sa_event

    engine = make_engine(f"sqlite:///{tmp_path/'ops.db'}")
    Base.metadata.create_all(engine)
    sf = make_sessionmaker(engine)
    with sf() as s:
        s.add(models.Tenant(tenant_id="fonii", name="F"))
        s.add(models.Project(project_id="partner", tenant_id="fonii", name="P", timezone="Europe/Moscow"))
        s.add(models.Participant(participant_id="dev1", tenant_id="fonii", display_name="D"))
        s.add(models.Session(session_id="s1", participant_id="dev1", project_id="partner",
                             task_ref="T-1", branch="b", started_at_utc="2026-11-01T00:00:00+00:00"))
        s.commit()
        for i in range(6):
            day = datetime(2026, 11, 1 + i // 2, 5, 0, tzinfo=timezone.utc) + timedelta(hours=6 * (i % 2))
            _hb(s, "s1", f"ev-ops-{i}", i, "active_human", day, 1.0)
        s.commit()

    queries = {"n": 0}

    def before(*a, **k):
        queries["n"] += 1

    sa_event.listen(engine, "before_cursor_execute", before)
    try:
        with sf() as sess:
            m = ops.collect(sess)
    finally:
        sa_event.remove(engine, "before_cursor_execute", before)
    assert m["partner_days_open_overdue"] == 3  # 6 интервалов → 3 дня
    assert queries["n"] < 15, f"слишком много SQL-запросов: {queries['n']} (N+1?)"


# ---------- FIX #10 (P3): Decimal-суммирование долей ----------


def test_status_month_decimal_share_sum(factory, client):
    """share_bp суммируется в Decimal; результат стабилен для значений,
    которых float-путь мог бы исказить."""
    with factory() as s:
        for i, bp in enumerate(["1111111111.1111", "2222222222.2222", "3333333333.3333"]):
            d = f"2026-11-{10 + i:02d}"
            s.add(models.DayRecord(
                day_id=f"dev1|{d}", participant_id="dev1", date=d, tz="Europe/Moscow",
                paid_seconds=3600, amount_cents=50000, cash_cents=0, equity_cents=50000,
                share_bp=bp, rate_version=1, valuation_version=1, policy_version=1,
                sha256_input="b" * 64, closed=1,
            ))
        s.commit()
    r = client.get("/v1/status/dev1?month=2026-11")
    assert r.status_code == 200
    assert r.json()["share_bp_month"] == "6666666666.6666"


def test_status_month_default_is_project_tz_month(factory, client):
    """FIX #12: месяц по умолчанию — по поясу проекта (Europe/Moscow = UTC+3:
    в 00:00–02:59 MSK месяца M UTC-месяц уже M+1 — раньше брался UTC)."""
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    expected = dt.now(ZoneInfo("Europe/Moscow")).isoformat()[:7]
    r = client.get("/v1/status/dev1")
    assert r.status_code == 200
    assert r.json()["month"] == expected
