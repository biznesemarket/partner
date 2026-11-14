"""Тесты юридического модуля (M7, development-plan.md §M7).

DoD M7: продукт полностью работает при legal.enabled: false; включение
модуля блокируется незакрытыми гейтами G-01…G-08; пакет юристу собирается
командой partner export-legal (через POST /v1/legal/export).

Фикстуры client/factory и helper moscow_local — в tests/conftest.py.
Файл состояния legal.yaml — tmp_path; настройки сервера (frozen dataclass)
переопределяются через object.__setattr__ (как в auth-тесте test_server.py).
"""
from __future__ import annotations

import hashlib
import itertools
import json

import pytest
from typer.testing import CliRunner

from conftest import moscow_local

from partner_server.routes import settings as routes_settings

DATE = "2026-11-12"
DATE_NEXT = "2026-11-13"
MONTH = "2026-11"
ALL_GATES = [f"G-0{i}" for i in range(1, 9)]

_SEQ = itertools.count()  # уникальный seq: события одной сессии не должны конфликтовать


@pytest.fixture()
def legal_file(tmp_path):
    """Файл состояния юрмодуля в tmp_path + подмена settings.legal_file."""
    path = tmp_path / "legal.yaml"
    old = routes_settings.legal_file
    object.__setattr__(routes_settings, "legal_file", str(path))
    yield path
    object.__setattr__(routes_settings, "legal_file", old)


def hb(event: str, cls: str, start_h: int, end_h: int, seq: int | None = None, date_offset: int = 0) -> dict:
    end_iso = moscow_local(end_h, date_offset_days=date_offset)
    return {
        "event_id": event,
        "session_id": "s1",
        "seq": next(_SEQ) if seq is None else seq,
        "cls": cls,
        "start_utc": moscow_local(start_h, date_offset_days=date_offset),
        "end_utc": end_iso,
        "received_at_utc": end_iso,
    }


def close_day(client, date: str, hours: int = 4) -> dict:
    """Один закрытый день dev1: hours активных часов, ставка 500 ₽/ч."""
    offset = 0 if date == DATE else (1 if date == DATE_NEXT else None)
    if offset is None:
        raise ValueError(f"дата {date} вне тестового окна conftest (BASE_DAY 2026-11-12)")
    client.post(
        "/v1/heartbeats",
        json=hb(f"ev-{date}", "active_human", 10, 10 + hours, date_offset=offset),
    )
    r = client.post(f"/v1/days/{date}/close", params={"participant_id": "dev1"})
    assert r.status_code == 200
    return r.json()


def clear_gates(client, by: str = "юрист") -> None:
    for gid in ALL_GATES:
        r = client.post(
            f"/v1/legal/gates/{gid}",
            json={"status": "done", "evidence": f"документ {gid}: подписан 2026-11-13, юрист",
                  "actor": by},
        )
        assert r.status_code == 200, r.text


# ---------- M7.3: гейты и включение модуля ----------


def test_default_all_gates_open(legal_file, client):
    r = client.get("/v1/legal")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert sorted(data["unclosed_gates"]) == ALL_GATES
    assert len(data["gates"]) == 8
    assert data["gates"][0]["gate_id"] == "G-01"
    assert data["gates"][0]["priority"] == "P0"
    assert "фантомная доля" in data["gates"][1]["subject"]


def test_product_works_without_legal(legal_file, client):
    """DoD M7: при выключенном модуле учет/дайджест/снапшоты работают."""
    closed = close_day(client, DATE, hours=4)
    assert closed["amount_cents"] == 200_000  # 4 ч × 500 ₽
    d = client.get(
        f"/v1/digest/day/{DATE}", params={"participant_id": "dev1"}
    ).json()
    assert "Начислено: 2000 ₽" in d["text"]
    snap = client.post(f"/v1/snapshots/{MONTH}", json={"actor": "директор"})
    assert snap.status_code == 200 and snap.json()["status"] == "created"


def test_enable_blocked_with_unclosed_gates(legal_file, client):
    r = client.post("/v1/legal/enable", json={"actor": "директор"})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"]["code"] == "legal_gates_not_cleared"
    assert "G-01" in body["detail"]["detail"] and "G-08" in body["detail"]["detail"]
    # модуль остался выключен; попытка зафиксирована как инцидент
    assert client.get("/v1/legal").json()["enabled"] is False
    audits = client.get("/v1/audit", params={"entity": "legal"}).json()
    assert any(e["action"] == "incident.legal_enable_blocked" for e in audits["entries"])


def test_gate_done_requires_evidence(legal_file, client):
    r = client.post(
        "/v1/legal/gates/G-01", json={"status": "done", "evidence": "", "actor": "юрист"}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "gate_evidence_required"
    # неизвестный гейт
    r = client.post(
        "/v1/legal/gates/G-09", json={"status": "done", "evidence": "документ", "actor": "юрист"}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_gate"


def test_gate_done_and_audit(legal_file, client):
    r = client.post(
        "/v1/legal/gates/G-04",
        json={"status": "done", "evidence": "модель угроз + флоу данных подписаны", "actor": "юрист"},
    )
    assert r.status_code == 200 and r.json()["status"] == "done"
    data = client.get("/v1/legal").json()
    assert data["unclosed_gates"] == [g for g in ALL_GATES if g != "G-04"]
    audits = client.get("/v1/audit", params={"entity": "legal"}).json()
    assert any(e["action"] == "gate.done" and e["entity_id"] == "G-04" for e in audits["entries"])


def test_enable_after_all_gates(legal_file, client):
    clear_gates(client)
    r = client.post("/v1/legal/enable", json={"actor": "юрист"})
    assert r.status_code == 200 and r.json()["status"] == "enabled"
    assert client.get("/v1/legal").json()["enabled"] is True
    # повторное включение — duplicate
    r = client.post("/v1/legal/enable", json={"actor": "юрист"})
    assert r.json()["status"] == "duplicate"
    audits = client.get("/v1/audit", params={"entity": "legal"}).json()
    assert any(e["action"] == "legal.enable" for e in audits["entries"])


# ---------- M7.1: месячные снапшоты ----------


def test_month_snapshot_matches_day_records(legal_file, client):
    d1 = close_day(client, DATE, hours=4)      # 2000 ₽
    d2 = close_day(client, DATE_NEXT, hours=2)  # 1000 ₽
    r = client.post(f"/v1/snapshots/{MONTH}", json={"actor": "директор"})
    assert r.status_code == 200
    snap = r.json()
    assert snap["status"] == "created" and snap["snapshot_id"] == f"snap-partner-{MONTH}"
    assert snap["git_ref"] is None or len(snap["git_ref"]) == 40

    detail = client.get(f"/v1/snapshots/{snap['snapshot_id']}").json()
    payload = detail["payload"]
    assert len(payload["participants"]) == 1
    p = payload["participants"][0]
    assert p["participant_id"] == "dev1"
    assert p["days"] == 2
    assert p["paid_seconds"] == d1["paid_seconds"] + d2["paid_seconds"]
    assert p["amount_cents"] == 300_000
    assert p["amount_rub"] == "3000"
    # sha256 — по canonical JSON payload, воспроизводимо
    sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert sha == snap["sha256"] == detail["sha256"]
    # подписываемый лист: доли, хеш, оговорка «не доля в ООО»
    assert "dev1" in detail["text"]
    assert "sha256 снапшота:" in detail["text"]
    assert "расчетная доля" in detail["text"]  # красная линия: не обещание доли в ООО
    # пул 30% = 3000 бп
    assert payload["pool_bp"] == "3000.0000"
    assert payload["policy_version"] == 1


def test_snapshot_duplicate_same_sha(legal_file, client):
    close_day(client, DATE, hours=4)
    first = client.post(f"/v1/snapshots/{MONTH}", json={"actor": "директор"}).json()
    second = client.post(f"/v1/snapshots/{MONTH}", json={"actor": "директор"}).json()
    assert second["status"] == "duplicate" and second["sha256"] == first["sha256"]


def test_snapshot_conflict_on_changed_month(legal_file, client):
    """Снапшот иммутабелен: тот же месяц с новыми числами — конфликт."""
    close_day(client, DATE, hours=4)
    assert client.post(f"/v1/snapshots/{MONTH}", json={"actor": "директор"}).status_code == 200
    close_day(client, DATE_NEXT, hours=2)
    r = client.post(f"/v1/snapshots/{MONTH}", json={"actor": "директор"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "snapshot_conflict"


def test_snapshot_empty_month(legal_file, client):
    r = client.post("/v1/snapshots/2026-10", json={"actor": "директор"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "no_days"


def test_snapshot_bad_month(legal_file, client):
    r = client.post("/v1/snapshots/2026", json={"actor": "директор"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "bad_month"


# ---------- M7.2: экспорт юристу ----------


def _prepare_export(client) -> dict:
    close_day(client, DATE, hours=4)
    client.post(f"/v1/snapshots/{MONTH}", json={"actor": "директор"})
    client.post(
        "/v1/legal/gates/G-05",
        json={"status": "done", "evidence": "CLA до первого вклада подписан", "actor": "юрист"},
    )
    r = client.post("/v1/legal/export", json={"actor": "директор"})
    assert r.status_code == 200
    return r.json()


def test_export_bundle_content(legal_file, client):
    bundle = _prepare_export(client)
    # политика без персональных данных
    assert bundle["policy"]["policy_version"] == 1
    assert "rate_rub_per_hour" in bundle["policy"]["params"]
    assert "display_name" not in json.dumps(bundle, ensure_ascii=False)
    # снапшот с подписываемым листом
    assert len(bundle["snapshots"]) == 1
    s = bundle["snapshots"][0]
    assert s["month"] == MONTH and s["text"] and s["sha256"]
    # гейты: G-05 снят, остальные открыты
    statuses = {g["gate_id"]: g["status"] for g in bundle["gates"]}
    assert statuses == {g: ("done" if g == "G-05" else "open") for g in ALL_GATES}
    # вопросы юристу и шаблоны M7.4
    assert len(bundle["questions_to_lawyer"]) == 6
    assert set(bundle["templates"]) == {
        "templates/participant-agreement.md",
        "templates/option-agreement.md",
        "templates/loan-agreement.md",
        "templates/newco-fixation-checklist.md",
    }
    # manifest: подписываемый sha256 = хеш canonical JSON ядра пакета
    core = {k: bundle[k] for k in ("policy", "snapshots", "gates", "legal_enabled")}
    sha = hashlib.sha256(
        json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert bundle["manifest"]["bundle_sha256"] == sha
    assert bundle["legal_enabled"] is False


def test_export_audit(legal_file, client):
    _prepare_export(client)
    audits = client.get("/v1/audit", params={"entity": "legal"}).json()
    assert any(e["action"] == "legal.export" for e in audits["entries"])


def test_cli_export_legal_writes_files(legal_file, client, tmp_path, monkeypatch):
    """CLI partner export-legal --to lawyer/ раскладывает пакет в каталог."""
    bundle = _prepare_export(client)
    from partner_cli import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_server_request", lambda *a, **kw: bundle)
    runner = CliRunner()
    target = tmp_path / "lawyer"
    result = runner.invoke(
        cli_mod.app, ["export-legal", "--to", str(target), "--by", "директор"]
    )
    assert result.exit_code == 0, result.output
    for rel in (
        "policy.json", "policy.md", f"snapshots/{MONTH}.md", "snapshots.json",
        "gates.md", "questions-to-lawyer.md", "MANIFEST.txt",
        "templates/participant-agreement.md", "templates/option-agreement.md",
        "templates/loan-agreement.md", "templates/newco-fixation-checklist.md",
    ):
        assert (target / rel).exists(), f"нет файла {rel}"
    snap_sheet = (target / f"snapshots/{MONTH}.md").read_text(encoding="utf-8")
    assert "Подпись директора" in snap_sheet
    assert bundle["manifest"]["bundle_sha256"] in (target / "MANIFEST.txt").read_text(encoding="utf-8")
    # шаблон M7.4: редакционный, без обещания доли в ООО до фиксации
    agreement = (target / "templates/participant-agreement.md").read_text(encoding="utf-8")
    assert "расчетную долю" in agreement and "шаблон без юридической силы" in agreement.lower()


def test_cli_legal_gates_table(legal_file, client, monkeypatch):
    from partner_cli import cli as cli_mod

    state = client.get("/v1/legal").json()
    monkeypatch.setattr(cli_mod, "_server_request", lambda *a, **kw: state)
    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["legal-gates"])
    assert result.exit_code == 0, result.output
    assert "G-01" in result.output and "G-08" in result.output
    assert "выключен" in result.output
