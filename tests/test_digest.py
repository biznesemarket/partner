"""Тесты M6: дайджесты, каналы доставки, спор дня (development-plan.md §6.x).

DoD M6: дайджест дня содержит только числа, воспроизводимые из БД;
спор и корректировка проходят полный цикл по документации.
"""
from __future__ import annotations


from conftest import moscow_local

from partner_server import models, service

DATE = "2026-11-12"
MONTH = "2026-11"


def hb(event: str, cls: str, start_h: int, end_h: int, seq: int, date_offset: int = 0) -> dict:
    end_iso = moscow_local(end_h, date_offset_days=date_offset)
    return {
        "event_id": event,
        "session_id": "s1",
        "seq": seq,
        "cls": cls,
        "start_utc": moscow_local(start_h, date_offset_days=date_offset),
        "end_utc": moscow_local(end_h, date_offset_days=date_offset),
        "received_at_utc": end_iso,
    }


def seed_and_close(client, date=DATE):
    """Стандартный день T-01 (6,6 ч → 3 300 ₽) и его закрытие."""
    client.post("/v1/heartbeats", json=hb("e1", "active_human", 10, 14, 0))
    client.post("/v1/heartbeats", json=hb("e2", "supervised_agent", 14, 16, 1))
    client.post("/v1/heartbeats", json=hb("e3", "autonomous_agent", 16, 19, 2))
    closed = client.post(f"/v1/days/{date}/close", params={"participant_id": "dev1"}).json()
    assert closed["closed"] is True
    return closed


# ---------- M6.1 дневной дайджест ----------


def test_day_digest_reproducible_from_db(client, factory):
    closed = seed_and_close(client)

    r = client.get(f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"})
    assert r.status_code == 200
    d = r.json()

    # DoD: числа дайджеста равны записи БД (закрытый день — append-only)
    with factory() as sess:
        rec = service.find_day_record(sess, sess.get(models.Project, "partner"), "dev1", DATE)
        assert d["paid_seconds"] == rec.paid_seconds
        assert d["amount_cents"] == rec.amount_cents
        assert d["amount_rub"] == "3300"
        assert d["share_bp"] == rec.share_bp == "3.3000"

    # часы по классам: пересборка из интервалов БД (T-01: 4 + 2 + 0.6 = 6,6)
    assert d["hours_by_class"] == {
        "active_human": "4",
        "supervised_agent": "2.0",
        "autonomous_agent": "0.6",
    }
    assert d["effective_amount_cents"] == closed["amount_cents"]  # без корректировок
    # детерминированный текст
    assert d["text"] == d["text"].strip()
    assert "3300" in d["text"] and "6.6" in d["text"]


def test_day_digest_deterministic_render(client):
    seed_and_close(client)
    a = client.get(f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"}).json()["text"]
    b = client.get(f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"}).json()["text"]
    assert a == b


def test_day_digest_not_closed(client):
    r = client.get(f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "day_not_closed"


def test_day_digest_mismatch_detected(client, factory):
    """Порча day_record обнаруживается: пересчет расходится с записью."""
    seed_and_close(client)
    with factory() as sess:
        rec = service.find_day_record(sess, sess.get(models.Project, "partner"), "dev1", DATE)
        rec.amount_cents = 999_999
        sess.commit()
    r = client.get(f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "day_mismatch"


# ---------- M6.2 месячный дайджест ----------


def test_month_digest_sums_and_pool(client):
    client.post("/v1/heartbeats", json=hb("a1", "active_human", 10, 14, 0))
    client.post("/v1/heartbeats", json=hb("a2", "active_human", 10, 12, 1, date_offset=1))
    client.post(f"/v1/days/{DATE}/close", params={"participant_id": "dev1"})
    client.post("/v1/days/2026-11-13/close", params={"participant_id": "dev1"})

    m = client.get(
        "/v1/digest/month/2026-11", params={"participant_id": "dev1"}
    ).json()
    # 4 ч (2 000 ₽) + 2 ч (1 000 ₽) = 3 000 ₽
    assert m["amount_cents"] == 300_000
    assert m["paid_hours"] == "6"
    assert m["days"] == 2
    # доли: 3000 ₽ / 10 000 000 ₽ = 0.03% = 3 бп
    assert m["share_bp"] == "3.0000"
    # остаток пула: 30% × 10 000 бп = 3 000 бп − 3 бп = 2 997 бп
    assert m["pool_bp"] == "3000.0000"
    assert m["accrued_bp_month"] == "3.0000"
    assert m["pool_remainder_bp"] == "2997.0000"
    # pending: незакрытых дней нет, споров нет
    assert m["pending"] == {"open_disputes": [], "unclosed_days": []}


def test_month_digest_pending_unclosed_day(client):
    client.post("/v1/heartbeats", json=hb("a1", "active_human", 10, 14, 0))
    client.post(f"/v1/days/{DATE}/close", params={"participant_id": "dev1"})
    # интервал на 13-е не закрыт
    client.post("/v1/heartbeats", json=hb("a2", "active_human", 10, 12, 1, date_offset=1))
    m = client.get(
        "/v1/digest/month/2026-11", params={"participant_id": "dev1"}
    ).json()
    assert m["pending"]["unclosed_days"] == ["2026-11-13"]


def test_month_digest_no_days(client):
    r = client.get("/v1/digest/month/2026-11", params={"participant_id": "dev1"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "no_days"


# ---------- M6.3 каналы доставки ----------


def test_day_digest_delivered_via_agent_chat(client):
    """После закрытия дня дайджест доставлен основным каналом agent_chat."""
    seed_and_close(client)
    digest_rows = [
        x
        for x in client.get("/v1/notifications").json()["notifications"]
        if x["kind"] == "day_digest"
    ]
    assert digest_rows, "дайджест дня должен попасть в очередь после close"
    row = digest_rows[0]
    assert row["status"] == "sent"
    assert row["channel"] == "agent_chat"
    assert row["attempts"] == 1
    assert DATE in row["text"] and "3300" in row["text"]


def test_notify_retry_then_failed(monkeypatch):
    """Недоставка: канал не настроен → pending с attempts; исчерпание → failed.

    Чистая БД без HTTP: queue → deliver с отказом канала agent_chat.
    """
    import pathlib
    import tempfile

    from partner_server import notify
    from partner_server.db import Base, make_engine, make_sessionmaker

    tmp = pathlib.Path(tempfile.mkdtemp()) / "ntf.db"
    engine = make_engine(f"sqlite:///{tmp}")
    Base.metadata.create_all(engine)
    sf = make_sessionmaker(engine)
    with sf() as sess:
        n_id = notify.queue(sess, kind="day_digest", recipient="dev1", payload={}, text="тест")

        def _broken(sess_, n_):
            raise RuntimeError("транспорт недоступен")

        saved = notify._SENDERS["agent_chat"]
        notify._SENDERS["agent_chat"] = _broken
        try:
            n = sess.get(models.Notification, n_id)
            assert notify.deliver(sess, n) is False
            n = sess.get(models.Notification, n_id)
            assert n.status == "pending" and n.attempts == 1 and "agent_chat" in n.last_error
            assert notify.deliver(sess, sess.get(models.Notification, n_id)) is False
            n = sess.get(models.Notification, n_id)
            assert n.attempts == 2
            assert notify.deliver(sess, sess.get(models.Notification, n_id)) is False
            n = sess.get(models.Notification, n_id)
            assert n.status == "failed" and n.attempts == 3  # notify_max_attempts=3
        finally:
            notify._SENDERS["agent_chat"] = saved


def test_monthly_dispatch_queues_all_participants(client):
    """dispatch_monthly: месячный дайджест в очередь + доставка agent_chat."""
    seed_and_close(client)
    from partner_server import notify
    from partner_server.routes import get_db

    override = client.app.dependency_overrides[get_db]
    sess = next(override())
    project = sess.query(models.Project).first()
    try:
        ids = notify.dispatch_monthly(sess, project, MONTH)
        assert ids, "месячный дайджест должен ставиться в очередь"
        row = sess.get(models.Notification, ids[0])
        assert row.kind == "month_digest"
        assert row.status == "sent" and row.channel == "agent_chat"
        assert "Итог месяца 2026-11" in row.text
        assert "3300" in row.text
    finally:
        sess.close()


# ---------- M6.4 спор дня ----------


def test_dispute_full_cycle_with_adjustment(client):
    closed = seed_and_close(client)
    base_amount = closed["amount_cents"]  # 330_000

    r = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1",
              "reason": "агент был выключен 17:00-18:00, интервал лишний",
              "interval_ids": ["e3"]},
    )
    assert r.status_code == 201
    dsp = r.json()
    assert dsp["status"] == "created"
    assert dsp["window_deadline"].endswith("+00:00")

    # второй открытый спор на тот же день запрещен
    r2 = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1", "reason": "повторный спор"},
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "dispute_already_open"

    # директор одобряет корректировку −100 ₽ (10 000 коп.)
    rr = client.post(
        f"/v1/disputes/{dsp['dispute_id']}/resolve",
        json={"decided_by": "директор", "decision": "approved",
              "delta_cents": -10_000, "resolution": "подтверждено: агент был выключен"},
    )
    assert rr.status_code == 200
    assert rr.json()["status"] == "resolved"
    adjustment_id = rr.json()["adjustment_id"]

    # день не переписан: adjustment поверх закрытого дня
    day = client.get(f"/v1/days/{DATE}", params={"participant_id": "dev1"}).json()
    assert day["amount_cents"] == base_amount  # исходная запись неизменна
    assert day["adjustments_delta_cents"] == -10_000
    assert day["effective_amount_cents"] == base_amount - 10_000
    assert day["effective_amount_rub"] == "3200"
    assert day["disputes"][0]["status"] == "resolved"

    # дайджест видит корректировку и спорный интервал
    d = client.get(f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"}).json()
    assert d["effective_amount_cents"] == base_amount - 10_000
    assert d["disputed_intervals"] == ["e3"]
    assert d["adjustments"][0]["adjustment_id"] == adjustment_id

    # уведомление участнику о решении
    rows = [x for x in client.get("/v1/notifications").json()["notifications"]
            if x["kind"] == "dispute_resolved"]
    assert rows and rows[0]["recipient"] == "dev1"

    # аудит цикла спора
    actions = {e["action"] for e in client.get("/v1/audit").json()["entries"]}
    assert "dispute.opened" in actions and "dispute.resolved" in actions

    # повторное решение по закрытому спору невозможно
    r3 = client.post(
        f"/v1/disputes/{dsp['dispute_id']}/resolve",
        json={"decided_by": "директор", "decision": "rejected"},
    )
    assert r3.status_code == 422
    assert r3.json()["detail"]["code"] == "dispute_not_open"
    assert adjustment_id


def test_dispute_rejected_no_adjustment(client):
    seed_and_close(client)
    r = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1", "reason": "не согласен с капом"},
    )
    dsp = r.json()
    rr = client.post(
        f"/v1/disputes/{dsp['dispute_id']}/resolve",
        json={"decided_by": "директор", "decision": "rejected", "resolution": "кап по политике"},
    )
    assert rr.json()["status"] == "rejected"
    day = client.get(f"/v1/days/{DATE}", params={"participant_id": "dev1"}).json()
    assert day["adjustments"] == [] and day["effective_amount_cents"] == day["amount_cents"]


def test_dispute_window_expired(client, factory):
    """Окно 3 дня: решение после дедлайна невозможно, спор → expired."""
    seed_and_close(client)
    r = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1", "reason": "поздний спор"},
    )
    dsp = r.json()
    with factory() as sess:
        d = sess.get(models.Dispute, dsp["dispute_id"])
        d.window_deadline = "2026-11-09T00:00:00+00:00"  # дедлайн в прошлом
        sess.commit()
    rr = client.post(
        f"/v1/disputes/{dsp['dispute_id']}/resolve",
        json={"decided_by": "директор", "decision": "approved", "delta_cents": 1},
    )
    assert rr.status_code == 422
    assert rr.json()["detail"]["code"] == "dispute_window_expired"
    with factory() as sess:
        assert sess.get(models.Dispute, dsp["dispute_id"]).status == "expired"


def test_dispute_open_on_unclosed_day(client):
    client.post("/v1/heartbeats", json=hb("e1", "active_human", 10, 14, 0))
    r = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1", "reason": "день еще не закрыт"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "day_not_closed"


def test_dispute_unknown_interval(client):
    seed_and_close(client)
    r = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1",
              "reason": "чужой интервал", "interval_ids": ["no-such-interval"]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_intervals"


def test_dispute_approved_without_delta(client):
    seed_and_close(client)
    r = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1", "reason": "спор без суммы"},
    )
    dsp = r.json()
    rr = client.post(
        f"/v1/disputes/{dsp['dispute_id']}/resolve",
        json={"decided_by": "директор", "decision": "approved"},
    )
    assert rr.status_code == 422
    assert rr.json()["detail"]["code"] == "delta_required"


def test_expire_overdue_marks_stale_disputes(client, factory):
    """Джоба истечения: открытый спор с прошедшим дедлайном → expired."""
    from partner_server import disputes as dsp_mod

    seed_and_close(client)
    r = client.post(
        f"/v1/days/{DATE}/disputes",
        json={"participant_id": "dev1", "opened_by": "dev1", "reason": "забытый спор"},
    )
    dsp_id = r.json()["dispute_id"]
    with factory() as sess:
        d = sess.get(models.Dispute, dsp_id)
        d.window_deadline = "2026-11-09T00:00:00+00:00"
        sess.commit()
    with factory() as sess:
        expired = dsp_mod.expire_overdue(sess)
        assert dsp_id in expired
        assert sess.get(models.Dispute, dsp_id).status == "expired"


# ---------- CLI (спор через сервер) ----------


def test_cli_dispute_requires_server_url(monkeypatch):
    """Без PARTNER_SERVER_URL команда dispute падает с понятной ошибкой."""
    from typer.testing import CliRunner

    from partner_cli.cli import app as cli_app

    monkeypatch.delenv("PARTNER_SERVER_URL", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli_app, ["dispute", DATE, "--reason", "тест", "--by", "dev1"]
    )
    assert result.exit_code == 1
    assert "PARTNER_SERVER_URL" in result.output
