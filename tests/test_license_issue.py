"""Тесты вендорского инструмента выдачи лицензий (M9.1, tools/license_issue.py).

Сценарии:
- LI-01  issue: ключ и секрет выдаются, запись попадает в реестр,
         секрет по умолчанию генерируется (per-tenant)
- LI-02  продление: повторный issue того же tenant переиспользует
         секрет из реестра (если --secret не задан)
- LI-03  list: записи видны, подпись перепроверяется (OK)
- LI-04  verify: выданный ключ проверяется против секрета реестра
- LI-05  verify: подделанный ключ — exit 1
- LI-06  verify: чужой секрет — exit 1
- LI-07  issue: дата в прошлом — отказ (exit 1)
- LI-08  key проверяется штатным licensing.current_state (совместимость)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools" / "license_issue.py"

SECRET_A = "aa" * 32
SECRET_B = "bb" * 32


def run(args: list[str], registry: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(TOOLS), "--registry", str(registry), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False,
    )


def future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _issue(registry: Path, tenant: str = "acme", edition: str = "pro",
           secret: str | None = None, expires: str | None = None) -> subprocess.CompletedProcess:
    args = ["issue", "--tenant", tenant, "--edition", edition,
            "--expires", expires or future(365)]
    if secret is not None:
        args += ["--secret", secret]
    return run(args, registry)


def _key_from(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("PARTNER_LICENSE_KEY="):
            return line.split("=", 1)[1]
    raise AssertionError("ключ не найден в выводе issue:\n" + out)


def _secret_from(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("PARTNER_LICENSE_SECRET="):
            return line.split("=", 1)[1]
    raise AssertionError("секрет не найден в выводе issue:\n" + out)


@pytest.fixture()
def registry(tmp_path: Path) -> Path:
    return tmp_path / "license-registry.json"


def test_li01_issue_generates_per_tenant_secret(registry: Path):
    res = _issue(registry)
    assert res.returncode == 0, res.stderr
    key, secret = _key_from(res.stdout), _secret_from(res.stdout)
    assert key.startswith("PARTNER.acme.pro.")
    assert len(secret) == 64
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert len(data["records"]) == 1
    rec = data["records"][0]
    assert (rec["tenant_id"], rec["edition"], rec["secret"]) == ("acme", "pro", secret)
    # права 0600: секреты клиентов не для групп/посторонних.
    # На Windows POSIX-биты не отражаются в st_mode — проверка только на POSIX.
    if os.name != "nt":
        assert (registry.stat().st_mode & 0o077) == 0


def test_li02_renewal_reuses_registry_secret(registry: Path):
    first = _issue(registry, secret=SECRET_A)
    assert first.returncode == 0, first.stderr
    renew = _issue(registry, expires=future(730))  # без --secret
    assert renew.returncode == 0, renew.stderr
    assert _secret_from(renew.stdout) == SECRET_A  # секрет повторно из реестра
    assert _key_from(renew.stdout) != _key_from(first.stdout)  # срок изменился
    assert len(json.loads(registry.read_text())["records"]) == 2  # история хранится


def test_li03_list_checks_signatures(registry: Path):
    _issue(registry, tenant="acme", secret=SECRET_A)
    _issue(registry, tenant="beata", edition="basic", secret=SECRET_B)
    res = run(["list"], registry)
    assert res.returncode == 0, res.stderr
    assert "acme" in res.stdout and "beata" in res.stdout
    assert res.stdout.count("OK") == 2  # обе подписи сходятся
    assert "active" in res.stdout


def test_li04_verify_roundtrip(registry: Path):
    res_issue = _issue(registry, secret=SECRET_A)
    key = _key_from(res_issue.stdout)
    res = run(["verify", "--key", key], registry)  # секрет — из реестра
    assert res.returncode == 0, res.stderr
    assert "подпись OK" in res.stdout
    assert "tenant=acme" in res.stdout and "active" in res.stdout


def test_li05_verify_rejects_tampered_key(registry: Path):
    key = _key_from(_issue(registry, secret=SECRET_A).stdout)
    head, sig = key.rsplit(".", 1)
    tampered = head + "." + ("0" if sig[0] != "0" else "1") + sig[1:]
    res = run(["verify", "--key", tampered], registry)
    assert res.returncode == 1
    assert "НЕ СХОДИТСЯ" in res.stderr


def test_li06_verify_rejects_wrong_secret(registry: Path):
    key = _key_from(_issue(registry, secret=SECRET_A).stdout)
    res = run(["verify", "--key", key, "--secret", SECRET_B], registry)
    assert res.returncode == 1
    assert "НЕ СХОДИТСЯ" in res.stderr


def test_li07_issue_rejects_past_date(registry: Path):
    past = (date.today() - timedelta(days=1)).isoformat()
    res = _issue(registry, expires=past)
    assert res.returncode == 1
    assert not registry.exists()  # реестр не создан — запись не добавлена


def test_li08_issued_key_valid_for_server_licensing(registry: Path):
    from partner_server import licensing
    from partner_server.config import Settings

    key = _key_from(_issue(registry, tenant="fonii", edition="pro").stdout)
    secret = json.loads(registry.read_text())["records"][0]["secret"]
    st = licensing.current_state(Settings(license_key=key, license_secret=secret))
    assert st.state == "active"
    assert st.tenant_id == "fonii" and st.edition == "pro"
