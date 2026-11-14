#!/usr/bin/env python3
"""M9.3 / D9: сборка публичного docs-сайта из репозитория.

Публикуется ТОЛЬКО approved-контент (docs-plan.md §5, D9 DoD):

1. Статус берется из таблицы метаданных самого документа (строка
   «| Статус | …» — approved/Принято/Действует).
2. Если в документе строки нет — из реестра docs-plan.md §5.
3. Нет нигде — документ не публикуется (безопасный дефолт).

Генератор — чистый stdlib + markdown-it-py (без ручной правки контента):
сайт рендерится из репозитория, CI строит артефакт docs-site/.

Запуск: python tools/build_docs_site.py [--out docs-site] [--root .]
"""
from __future__ import annotations

import argparse
import html
import pathlib
import re
import shutil

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover
    raise SystemExit("нужен markdown-it-py: pip install markdown-it-py")

APPROVED_MARKERS = ("approved", "принято", "действует", "реализовано")
DRAFT_MARKERS = ("draft", "черновик", "на утверждении")
SITE_CSS = """
:root { --ink:#1a1d21; --paper:#ffffff; --accent:#0b62c4; --line:#e3e7ec; }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.65 -apple-system,'Segoe UI',Roboto,sans-serif; color:var(--ink); background:var(--paper); }
header { border-bottom:1px solid var(--line); padding:18px 24px; display:flex; gap:16px; align-items:baseline; }
header h1 { font-size:20px; margin:0; }
header nav a, .crumbs a { color:var(--accent); text-decoration:none; margin-right:12px; }
.wrap { display:flex; max-width:1100px; margin:0 auto; }
aside { width:260px; border-right:1px solid var(--line); padding:20px 16px; min-height:80vh; }
aside h2 { font-size:13px; text-transform:uppercase; color:#6b7480; margin:18px 0 6px; }
aside a { display:block; color:var(--ink); text-decoration:none; padding:3px 6px; border-radius:4px; font-size:14px; }
aside a:hover { background:#f2f6fb; color:var(--accent); }
main { flex:1; padding:28px 36px; max-width:760px; }
main h1 { font-size:26px; } main h2 { font-size:20px; margin-top:28px; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:14px; }
th, td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }
th { background:#f6f8fa; }
code { background:#f2f4f7; padding:1px 5px; border-radius:4px; font-size:.92em; }
pre { background:#10151b; color:#e6edf3; padding:14px 16px; border-radius:8px; overflow-x:auto; }
pre code { background:transparent; color:inherit; }
footer { border-top:1px solid var(--line); padding:14px 24px; color:#6b7480; font-size:13px; }
"""


def parse_status(text: str) -> str | None:
    """Статус из таблицы метаданных документа: строка «| Статус | …»."""
    m = re.search(r"^\|\s*Статус\s*\|\s*(.+?)\s*\|\s*$", text, re.M)
    return m.group(1) if m else None


def _has_token(status: str) -> bool:
    low = status.lower()
    return any(m in low for m in APPROVED_MARKERS + DRAFT_MARKERS)


def resolve_status(text: str, registry: dict[str, str], rel: str) -> str | None:
    """Собственная строка «| Статус | …» если она содержит токен статуса,
    иначе реестр docs-plan.md; нет нигде — None (не публикуется)."""
    own = parse_status(text)
    if own and _has_token(own):
        return own
    return registry.get(rel)


def parse_registry(root: pathlib.Path) -> dict[str, str]:
    """Статусы из реестра docs-plan.md §5: | путь | аудитория | Статус | …"""
    plan = root / "docs" / "00-start" / "docs-plan.md"
    out: dict[str, str] = {}
    if not plan.exists():
        return out
    for line in plan.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[0].startswith("docs/") and cells[0].endswith(".md"):
            out[cells[0]] = cells[2]
    return out


def is_approved(status: str | None) -> bool:
    if not status:
        return False
    low = status.lower()
    return any(marker in low for marker in APPROVED_MARKERS)


def title_of(text: str, path: pathlib.Path) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else path.stem


def group_of(rel: str) -> str:
    return rel.split("/")[1] if rel.count("/") >= 1 else "docs"


GROUP_TITLES = {
    "00-start": "Начало работы",
    "01-product": "Продукт",
    "02-policy": "Правила учета и долей",
    "03-architecture": "Архитектура",
    "04-security": "Безопасность",
    "05-process": "Процессы",
    "06-legal": "Юридический модуль",
    "07-ops": "Эксплуатация",
    "08-guides": "Руководства",
    "10-gtm": "Поставки и тарифы",
}


def build(root: pathlib.Path, out: pathlib.Path) -> tuple[int, int]:
    md = MarkdownIt("commonmark").enable("table")
    registry = parse_registry(root)
    docs = sorted(p for p in (root / "docs").rglob("*.md"))

    published: list[tuple[str, str]] = []  # (rel posix, title)
    skipped = 0
    rendered: dict[str, str] = {}

    for path in docs:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        status = resolve_status(text, registry, rel)
        if not is_approved(status):
            skipped += 1
            continue
        body_html = md.render(text)
        rendered[rel] = body_html
        published.append((rel, title_of(text, path)))

    if out.exists():
        shutil.rmtree(out)
    for rel, _ in published:
        page = _page(rel, rendered[rel], published, root)
        target = out / (rel.replace("/", "__").removesuffix(".md") + ".html")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")

    (out / "index.html").write_text(_index(published), encoding="utf-8")
    return len(published), skipped


def _nav(published: list[tuple[str, str]], current: str | None = None) -> str:
    groups: dict[str, list[tuple[str, str]]] = {}
    for rel, title in published:
        groups.setdefault(group_of(rel), []).append((rel, title))
    parts = []
    for g in sorted(groups):
        label = GROUP_TITLES.get(g, g)
        parts.append(f"<h2>{html.escape(label)}</h2>")
        for rel, title in groups[g]:
            href = rel.replace("/", "__").removesuffix(".md") + ".html"
            cls = ' class="active"' if rel == current else ""
            parts.append(f'<a href="{href}"{cls}>{html.escape(title)}</a>')
    return "\n".join(parts)


def _page(rel: str, body: str, published, root: pathlib.Path) -> str:
    title = next(t for r, t in published if r == rel)
    crumbs = " / ".join(
        f'<a href="index.html">{_b}</a>' if i == 0 else html.escape(_b)
        for i, _b in enumerate(rel.split("/")[:-1])
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Партнер · docs</title>
<style>{SITE_CSS}</style></head>
<body>
<header><h1>Партнер — документация</h1>
<nav><a href="index.html">Все документы</a></nav></header>
<div class="wrap">
<aside>{_nav(published, rel)}</aside>
<main><div class="crumbs">{crumbs}</div>
{body}
</main></div>
<footer>Партнер · самохост-учет времени работы с ИИ-агентами ·
публикуется только approved-контент репозитория (docs-plan.md §5)</footer>
</body></html>"""


def _index(published: list[tuple[str, str]]) -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Партнер — документация</title>
<style>{SITE_CSS}</style></head>
<body>
<header><h1>Партнер — документация</h1>
<nav><a href="https://example.local" rel="noopener">Продукт</a></nav></header>
<div class="wrap">
<aside>{_nav(published)}</aside>
<main>
<h1>Документация «Партнера»</h1>
<p>Самохост-система учета времени работы разработчика с ИИ-агентами и
ежедневного начисления денежных вознаграждений и проектных долей
(фантомный динамический пул 30%). Установка —
<a href="docs__07-ops__installation.html">installation.md</a>,
эксплуатация — <a href="docs__07-ops__runbook.html">runbook.md</a>.</p>
<p>Публикуются только документы со статусом <strong>approved</strong>
(см. docs-plan.md §5). Всего опубликовано: {len(published)}.</p>
</main></div>
<footer>Партнер · docs-сайт собирается из репозитория автоматически (M9.3)</footer>
</body></html>"""


def main() -> int:
    from partner_cli.console_io import configure_stdio

    configure_stdio()
    ap = argparse.ArgumentParser(description="Сборка docs-сайта (только approved)")
    ap.add_argument("--root", default=".", help="корень репозитория")
    ap.add_argument("--out", default="docs-site", help="каталог вывода")
    args = ap.parse_args()
    root = pathlib.Path(args.root).resolve()
    out = pathlib.Path(args.out).resolve()
    n, skipped = build(root, out)
    print(f"docs-site: опубликовано {n}, пропущено (не approved) {skipped} → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
