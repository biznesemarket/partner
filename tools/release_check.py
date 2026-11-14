#!/usr/bin/env python3
"""M9.4: приемка релиза v1.0 — чек-лист development-plan.md §6 прогонами.

Проверяет, что можно проверить кодом:
  1. golden-тесты + интеграционные (pytest)
  2. линтер политики: позитивный прогон обоих примеров
  3. миграции: alembic upgrade head + alembic check (SQLite)
  4. docs-сайт собирается из репозитория (только approved)

Организационные пункты (установка у пилотного клиента, лицензионный ключ
в боевом контуре) отмечаются вручную — вывод указывает, что осталось.

Запуск: python tools/release_check.py [--write docs/05-process/release-acceptance.md]
Код возврата 0 — все автоматические пункты зеленые.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

AUTO_ITEMS = [
    ("19 golden-тестов T-01…T-12 зеленые против calc.py и сервера",
     "pytest"),
    ("Линтер политики: позитивные прогоны обоих примеров",
     "policy_lint"),
    ("Миграции: alembic upgrade head + соответствие моделям (alembic check)",
     "alembic"),
    ("Интеграционные тесты сервера (T-01…T-12 через HTTP, идемпотентность)",
     "pytest"),
    ("Антифрод-тесты replay / synthetic / night spike",
     "pytest"),
    ("Тесты эксплуатации M8 (метрики, бэкап-логика, dayclose_cli)",
     "pytest"),
    ("Тесты релиза M9 (лицензия, устройства, docs-сайт)",
     "pytest"),
    ("Docs-сайт собирается из репозитория, только approved (D9)",
     "site"),
    ("Снапшоты и юридический пакет: тесты M7 (гейты G-01…G-08)",
     "pytest"),
]

MANUAL_ITEMS = [
    "Установка по installation.md чистой машиной за ≤ 1 час",
    "Восстановление из бэкапа отрепетировано на стенде клиента (restore_check.sh)",
    "Лицензионный ключ работает, грейс-период проверен в боевом контуре",
    "Пилот у первого клиента подписан и запущен",
]


def _child_env() -> dict[str, str]:
    """UTF-8 в дочерних процессах: иначе на Windows cp1251 ломает заметки акта."""
    import os

    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(name: str) -> tuple[bool, str]:
    if name == "pytest":
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=REPO, env=_child_env(), capture_output=True, text=True,
            timeout=600, encoding="utf-8", errors="replace", check=False,
        )
        tail = (r.stdout or "").strip().splitlines()[-1:]
        return r.returncode == 0, (tail[0] if tail else "")
    if name == "policy_lint":
        outs = []
        for target in ("docs/02-policy/policy.example.yaml",
                       "repo-pack/policy.example.yaml"):
            r = subprocess.run(
                [sys.executable, "tools/policy_lint.py", target],
                cwd=REPO, env=_child_env(), capture_output=True, text=True,
                timeout=60, encoding="utf-8", errors="replace", check=False,
            )
            outs.append(f"{target}: {'OK' if r.returncode == 0 else 'FAIL'}")
        ok = all("OK" in o for o in outs)
        return ok, "; ".join(outs)
    if name == "alembic":
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            db = f"sqlite:///{pathlib.Path(tmp, 'release_check.db').as_posix()}"
            env = {**_child_env(), "PARTNER_DB_URL": db}
            r1 = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=REPO, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120, check=False,
            )
            r2 = subprocess.run(
                [sys.executable, "-m", "alembic", "check"],
                cwd=REPO, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120, check=False,
            )
            ok = r1.returncode == 0 and r2.returncode == 0
            return ok, ("upgrade head + check OK" if ok else "FAIL")
    if name == "site":
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, "tools/build_docs_site.py",
                 "--root", str(REPO), "--out", tmp],
                cwd=REPO, env=_child_env(), capture_output=True, text=True,
                timeout=120, encoding="utf-8", errors="replace", check=False,
            )
            # в акт — только счетчики, без временного пути
            note = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
            return r.returncode == 0, note.split("→")[0].strip()
    raise ValueError(name)


def main() -> int:
    from partner_cli.console_io import configure_stdio

    configure_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", default=None,
                    help="путь акта приемки (markdown) для записи результатов")
    args = ap.parse_args()

    results: list[tuple[str, bool, str]] = []
    all_ok = True
    print("Автоматические пункты чек-листа релиза (development-plan.md §6):\n")
    for title, probe in AUTO_ITEMS:
        ok, note = run(probe)
        all_ok &= ok
        results.append((title, ok, note))
        print(f"  [{'OK' if ok else 'FAIL'}] {title}  — {note}")

    print("\nОрганизационные пункты (отмечаются вручную у клиента):")
    for title in MANUAL_ITEMS:
        results.append((title, None, ""))
        print(f"  [ ] {title}")

    if args.write:
        _write_act(args.write, results)
        print(f"\nакт приемки записан: {args.write}")

    print(f"\nИтог автоматической части: {'ПРОЙДЕНА' if all_ok else 'ЕСТЬ СБОИ'}")
    return 0 if all_ok else 1


def _write_act(path: str, results) -> None:
    today = datetime.date.today().isoformat()
    lines = [
        "# Акт приемки релиза v1.0.0",
        "",
        "| Поле | Значение |",
        "|---|---|",
        f"| Дата автоматической приемки | {today} |",
        "| Инструмент | tools/release_check.py (development-plan.md §6) |",
        f"| Автоматическая часть | {'ПРОЙДЕНА' if all(ok is True for _, ok, _ in results if ok is not None) else 'ЕСТЬ СБОИ'} |",
        "",
        "## Автоматические проверки",
        "",
        "| Пункт чек-листа §6 | Результат | Примечание |",
        "|---|---|---|",
    ]
    for title, ok, note in results:
        if ok is None:
            continue
        lines.append(f"| {title} | {'OK' if ok else 'FAIL'} | {note} |")
    lines += [
        "",
        "## Организационные пункты (закрываются у пилотного клиента)",
        "",
        "| Пункт | Статус |",
        "|---|---|",
    ]
    for title, ok, _ in results:
        if ok is not None:
            continue
        lines.append(f"| {title} | ☐ при пилотной установке |")
    lines += [
        "",
        "Примечание: DoD M9 («установлен у пилотного клиента без участия",
        "разработчика») закрывается организационно — после поставки пакета",
        "релиза (deploy/scripts/release.sh) и подписания пилота; дата в акте",
        "автоматической части фиксирует состояние кода v1.0.0.",
        "",
        "## История изменений",
        "",
        "| Версия | Дата | Изменение |",
        "|---|---|---|",
        f"| 1.0 | {today} | Автоматическая приемка релиза v1.0.0 |",
        "",
    ]
    target = REPO / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
