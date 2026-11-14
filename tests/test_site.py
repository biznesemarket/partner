"""Тесты M9.3 (docs-сайт): сборка из репозитория, только approved.

Сценарии:
- S-01  сайт собирается: index.html + страницы; approved-документы опубликованы
- S-02  draft-документы НЕ публикуются (legal-module); overview и гайды D6
        опубликованы после v1.2
- S-03  страницы — валидный HTML без утечки сырого markdown таблиц
- S-04  resolve_status: собственный статус с токеном выигрывает у реестра
- S-05  C4 overview и гайды D6 содержат ключевые контракты и числа
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from build_docs_site import (  # noqa: E402
    build,
    is_approved,
    parse_registry,
    resolve_status,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    n, skipped = build(REPO, out)
    return out, n, skipped


# --- S-01 ----------------------------------------------------------------

def test_site_builds_with_index_and_pages(site):
    out, n, skipped = site
    assert (out / "index.html").exists()
    assert n >= 20, f"слишком мало approved-документов: {n}"
    assert skipped >= 3, "draft-фильтр не отработал"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "Партнер" in idx and "approved" in idx


# --- S-02 ----------------------------------------------------------------

def test_draft_docs_not_published(site):
    out, _, _ = site
    names = [p.name for p in out.glob("*.html")]
    assert not any("legal-module" in nm for nm in names)
    # approved есть, включая C4 overview и контракты v1.1/v1.2
    assert "docs__03-architecture__overview.html" in names
    assert "docs__03-architecture__api-reference.html" in names
    assert "docs__03-architecture__connectors.html" in names
    assert "docs__03-architecture__agent-kit.html" in names
    assert "docs__08-guides__morning-developer.html" in names
    assert "docs__08-guides__director-day.html" in names
    assert "docs__07-ops__installation.html" in names
    assert "docs__07-ops__runbook.html" in names
    assert "docs__02-policy__time-accounting.html" in names


# --- S-03 ----------------------------------------------------------------

def test_pages_render_html_not_raw_tables(site):
    out, _, _ = site
    page = (out / "docs__07-ops__installation.html").read_text(encoding="utf-8")
    assert "<table>" in page and "</td>" in page  # markdown отрендерен
    assert "| Поле | Значение |" not in page  # сырая таблица не утекла
    assert "<script" not in page.lower()  # без скриптов в контенте


# --- S-04 ----------------------------------------------------------------

def test_resolve_status_prefers_own_token():
    own = "| Поле | Значение |\n|---|---|\n| Статус | draft |\n"
    assert resolve_status(own, {"x": "approved"}, "x") == "draft"
    # без токена статуса — реестр
    no_token = "| Статус | план на D4 |\n"
    assert resolve_status(no_token, {"x": "approved"}, "x") == "approved"
    # токен без слова approved — всё равно распознается как approved-маркер
    assert resolve_status("| Статус | реализовано в M4 |\n", {"x": "approved"}, "x") == "реализовано в M4"
    assert is_approved("реализовано в M4")


def test_registry_parses_docs_plan():
    registry = parse_registry(REPO)
    assert registry.get("docs/07-ops/installation.md") == "approved"
    assert registry.get("docs/03-architecture/overview.md") == "approved"
    assert registry.get("docs/08-guides/morning-developer.md") == "approved"
    assert registry.get("docs/08-guides/director-day.md") == "approved"


# --- S-05 ----------------------------------------------------------------

def test_c4_and_guides_have_key_contracts(site):
    out, _, _ = site
    overview = (out / "docs__03-architecture__overview.html").read_text(encoding="utf-8")
    assert "C4 L1" in overview and "C4 L2" in overview and "C4 L3" in overview
    assert "service.ingest_heartbeat" in overview
    assert "ADR-0021" in overview
    morning = (out / "docs__08-guides__morning-developer.html").read_text(encoding="utf-8")
    assert "3 300" in morning and "0,0330" in morning
    # тексты дайджеста совпадают с render_day_digest (digest.py)
    assert "человек (active_human)" in morning
    assert "агент под наблюдением (supervised_agent)" in morning
    assert "PARTNER_GRACE_HOURS" in morning
    director = (out / "docs__08-guides__director-day.html").read_text(encoding="utf-8")
    assert "snapshot" in director and "3 суток" in director


def test_is_approved_markers():
    assert is_approved("approved")
    assert is_approved("Принято")
    assert is_approved("Действует (Python, PostgreSQL)")
    assert not is_approved("draft")
    assert not is_approved(None)
