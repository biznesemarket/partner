#!/usr/bin/env python3
"""Вендорский инструмент выдачи лицензионных ключей «Партнёр» (M9.1, ADR-0018).

НЕ входит в клиентскую поставку: хранится в приватном репозитории вендора.

Ключ (формат licensing.py):

    PARTNER.<tenant_id>.<edition>.<YYYY-MM-DD>.<подпись HMAC-SHA256>
    подпись = HMAC-SHA256(PARTNER_LICENSE_SECRET, "<tenant>|<edition>|<expires>")

Секрет поставки — per-tenant: при первой выдаче генерируется автоматически
(secrets.token_hex(32)) и сохраняется в реестре; при продлении того же
tenant секрет используется повторно (если не задан явно через --secret).
Секрет передается клиенту НЕ тем же каналом, что и ключ
(licensing-pricing.md §2).

Реестр выдачи (license-registry.json) — JSON со списком записей; каждая
запись — событие выдачи (продление добавляет новую запись, история
сохраняется). Файл содержит секреты клиентов: хранить в приватном
хранилище вендора, права 0600, в git и в клиентскую поставку не попадает.

Команды:

    issue   --tenant TEN --edition basic|pro|pilot --expires ГГГГ-ММ-ДД
            [--secret HEX] [--issuer ИМЯ] [--note ТЕКСТ] [--registry FILE]
            Выдать ключ (продление: тот же tenant — тот же секрет).

    list    [--tenant TEN] [--registry FILE]
            Сверка реестра: статус каждой записи, перепроверка подписи.

    verify  --key KEY [--secret HEX] [--registry FILE]
            Проверить произвольный ключ (секрет — из аргумента или реестра
            по tenant из ключа).

    secret  --tenant TEN [--registry FILE]
            Показать сохраненный секрет tenant (для повторной передачи
            другим каналом).

Состояния (licensing.current_state): active / grace / expired / invalid.
Выходные коды: 0 — успех; 1 — ошибка (проверка не сошлась, запись не найдена).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# лицензирование — единственный источник истины по формату ключа
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.partner_server.config import Settings  # noqa: E402
from apps.partner_server.licensing import (  # noqa: E402
    current_state,
    issue_key,
    signature,
)

REGISTRY_VERSION = 1
KNOWN_EDITIONS = ("basic", "pro", "pilot")


# ---------- реестр ----------

def load_registry(path: Path) -> dict:
    if not path.exists():
        return {"version": REGISTRY_VERSION, "records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SystemExit(f"ОШИБКА: реестр {path} поврежден ({err})")
    if "records" not in data:
        raise SystemExit(f"ОШИБКА: в {path} нет ключа records — не реестр?")
    return data


def save_registry(path: Path, data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def records_for(data: dict, tenant: str | None = None) -> list[dict]:
    recs = data["records"]
    if tenant is None:
        return recs
    return [r for r in recs if r["tenant_id"] == tenant]


def latest_record(data: dict, tenant: str) -> dict | None:
    recs = [r for r in data["records"] if r["tenant_id"] == tenant]
    if not recs:
        return None
    return recs[-1]  # записи хранятся в порядке выдачи


# ---------- статусы ----------

def status_line(record: dict, today: date) -> tuple[str, bool]:
    """(статус-строка, подпись_ок) для записи реестра."""
    sig_ok = signature(
        record["secret"], record["tenant_id"], record["edition"], record["expires"]
    ) == record["key"].rsplit(".", 1)[-1]
    st = current_state(
        Settings(
            license_key=record["key"],
            license_secret=record["secret"],
            license_required=True,
        ),
        today,
    )
    left = "" if st.days_left is None else f" ({st.days_left:+d} дн.)"
    if not sig_ok:
        return "ПОДПИСЬ НЕ СХОДИТСЯ", False
    return f"{st.state}{left}", True


def print_table(rows: list[list[str]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print("  ".join("-" * w for w in widths))


# ---------- команды ----------

def cmd_issue(args: argparse.Namespace) -> int:
    today = date.today()
    try:
        expires = date.fromisoformat(args.expires)
    except ValueError:
        print("ОШИБКА: --expires должен быть датой ГГГГ-ММ-ДД", file=sys.stderr)
        return 1
    if not args.tenant.strip():
        print("ОШИБКА: --tenant не может быть пустым", file=sys.stderr)
        return 1
    if args.edition not in KNOWN_EDITIONS:
        print(f"ПРЕДУПРЕЖДЕНИЕ: edition={args.edition!r} не из {KNOWN_EDITIONS} — тариф не из прайса")
    if expires < today:
        print("ОШИБКА: --expires в прошлом — ключ сразу будет в состоянии expired", file=sys.stderr)
        return 1

    path = Path(args.registry)
    data = load_registry(path)
    prev = latest_record(data, args.tenant)
    if args.secret:
        secret = args.secret
    elif prev:
        secret = prev["secret"]  # продление: per-tenant секрет сохраняется
        print(f"Продление {args.tenant}: секрет повторно взят из реестра (перевыдача не нужна)")
    else:
        secret = secrets.token_hex(32)  # per-tenant: компрометация не подделывает чужие ключи

    key = issue_key(secret, args.tenant, args.edition, args.expires)
    record = {
        "tenant_id": args.tenant.strip(),
        "edition": args.edition,
        "expires": args.expires,
        "key": key,
        "secret": secret,
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issuer": args.issuer or "",
        "note": args.note or "",
    }
    data["records"].append(record)
    save_registry(path, data)

    print()
    print("Ключ выдан и записан в реестр:", path)
    print()
    print("PARTNER_LICENSE_KEY=" + key)
    print()
    print("PARTNER_LICENSE_SECRET=" + secret)
    print()
    print("Клиенту: обе строки в deploy/.env + PARTNER_LICENSE_REQUIRED=1,")
    print("затем docker compose up -d server. Ключ и секрет передать РАЗНЫМИ")
    print("каналами; запись в реестре:", args.tenant, args.edition, args.expires)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    path = Path(args.registry)
    data = load_registry(path)
    recs = records_for(data, args.tenant)
    if not recs:
        scope = args.tenant or "реестре"
        print(f"Записей для {scope!r} нет")
        return 1
    today = date.today()
    rows = [["tenant", "edition", "expires", "статус", "выдан", "подпись"]]
    for r in recs:
        st, sig_ok = status_line(r, today)
        rows.append([
            r["tenant_id"], r["edition"], r["expires"], st,
            r["issued_at"][:10], "OK" if sig_ok else "ОШИБКА",
        ])
    print_table(rows)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    parts = args.key.split(".")
    if len(parts) != 5 or parts[0] != "PARTNER":
        print("ОШИБКА: формат ключа PARTNER.<tenant>.<edition>.<дата>.<подпись64>", file=sys.stderr)
        return 1
    tenant, edition, expires, sig = parts[1], parts[2], parts[3], parts[4]
    secret = args.secret
    if secret is None:
        rec = latest_record(load_registry(Path(args.registry)), tenant)
        if rec is None:
            print(f"ОШИБКА: секрета нет ни в --secret, ни в реестре для tenant={tenant!r}", file=sys.stderr)
            return 1
        secret = rec["secret"]
    try:
        expected = signature(secret, tenant, edition, expires)
    except ValueError:
        print("ОШИБКА: неверные данные ключа", file=sys.stderr)
        return 1
    if not secrets.compare_digest(sig, expected):
        print("Результат: ПОДПИСЬ НЕ СХОДИТСЯ — ключ подделан или секрет другой", file=sys.stderr)
        return 1
    st = current_state(
        Settings(license_key=args.key, license_secret=secret, license_required=True)
    )
    left = "" if st.days_left is None else f", дней до конца: {st.days_left:+d}"
    print(f"Результат: подпись OK; tenant={tenant} edition={edition} expires={expires}")
    print(f"Состояние: {st.state}{left} (grace-период клиента — PARTNER_LICENSE_GRACE_DAYS)")
    return 0


def cmd_secret(args: argparse.Namespace) -> int:
    rec = latest_record(load_registry(Path(args.registry)), args.tenant)
    if rec is None:
        print(f"ОШИБКА: записей для tenant={args.tenant!r} нет", file=sys.stderr)
        return 1
    print(rec["secret"])
    return 0


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="license_issue.py",
        description="Выдача и сверка лицензионных ключей «Партнёр» (вендорский инструмент, M9.1)",
    )
    p.add_argument(
        "--registry",
        default=os.getenv("PARTNER_LICENSE_REGISTRY", "license-registry.json"),
        help="файл реестра выдачи (по умолчанию ./license-registry.json или env PARTNER_LICENSE_REGISTRY)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("issue", help="выдать ключ (или продлить: тот же tenant)")
    pi.add_argument("--tenant", required=True, help="tenant_id клиента (напр. fonii)")
    pi.add_argument("--edition", required=True, help="тариф: basic | pro | pilot")
    pi.add_argument("--expires", required=True, help="срок ГГГГ-ММ-ДД")
    pi.add_argument("--secret", help="секрет поставки (иначе — из реестра или новый случайный)")
    pi.add_argument("--issuer", help="кто выдал (для журнала)")
    pi.add_argument("--note", help="заметка: тариф, договор №, пилот и т.п.")
    pi.set_defaults(func=cmd_issue)

    pl = sub.add_parser("list", help="сверка реестра: статусы и перепроверка подписей")
    pl.add_argument("--tenant", help="фильтр по tenant_id")
    pl.set_defaults(func=cmd_list)

    pv = sub.add_parser("verify", help="проверить произвольный ключ")
    pv.add_argument("--key", required=True, help="полный ключ PARTNER.…")
    pv.add_argument("--secret", help="секрет (иначе берется из реестра по tenant)")
    pv.set_defaults(func=cmd_verify)

    ps = sub.add_parser("secret", help="показать сохраненный секрет tenant")
    ps.add_argument("--tenant", required=True)
    ps.set_defaults(func=cmd_secret)
    return p


def main(argv: list[str] | None = None) -> int:
    from partner_cli.console_io import configure_stdio

    configure_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
