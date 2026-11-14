#!/usr/bin/env python3
"""D-05: OpenAPI-выжимка для клиента из фактического `app.openapi()`.

Генерирует `docs/03-architecture/api-reference.md`: группы эндпоинтов,
аутентификацию, схемы запросов и коды ошибок. Полная спецификация —
`GET /openapi.json`, интерактивная документация — `GET /docs`.

Запуск:
  python tools/openapi_digest.py            # записать файл
  python tools/openapi_digest.py --check    # проверить актуальность
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "03-architecture" / "api-reference.md"
METHOD_ORDER = ("get", "post", "put", "patch", "delete")
TAG_TITLES = {
    "system": "Система",
    "tracking": "Сессии и события",
    "devices": "Устройства",
    "days": "Дни",
    "status": "Статусы и флаги",
    "digest": "Дайджесты и уведомления",
    "disputes": "Споры",
    "policy": "Политика",
    "audit": "Аудит",
    "legal": "Юридический модуль и снапшоты",
    "connectors": "Коннекторы",
    "other": "Прочее",
}
TAG_ORDER = tuple(TAG_TITLES)


def _short(text: str | None, limit: int = 160) -> str:
    if not text:
        return "—"
    paragraph = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    line = re.sub(r"\s+", " ", paragraph).strip().replace("|", "\\|")
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def _operation_description(operation: dict) -> str:
    return _short(operation.get("description") or operation.get("summary"))


def _request_schemas(spec: dict) -> list[str]:
    schemas = spec.get("components", {}).get("schemas", {})
    names: set[str] = set()
    for operations in spec.get("paths", {}).values():
        for operation in operations.values():
            body = operation.get("requestBody") or {}
            for media in (body.get("content") or {}).values():
                ref = str((media.get("schema") or {}).get("$ref") or "")
                if ref.startswith("#/components/schemas/"):
                    names.add(ref.rsplit("/", 1)[1])
    return [name for name in sorted(names) if name in schemas]


def _endpoint_groups(spec: dict) -> dict[str, list[tuple[str, str, str]]]:
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for path, operations in spec.get("paths", {}).items():
        for method, operation in operations.items():
            if method.lower() not in METHOD_ORDER:
                continue
            tag = str((operation.get("tags") or ["other"])[0])
            groups.setdefault(tag, []).append(
                (method.upper(), path, _operation_description(operation))
            )
    return groups


def render_digest(spec: dict) -> str:
    info = spec.get("info", {})
    version = str(info.get("version", "?"))
    lines: list[str] = [
        "# API «Партнёра» — OpenAPI-выжимка",
        "",
        "| Поле | Значение |",
        "|---|---|",
        f"| Версия | {version} |",
        "| Назначение | Справочник клиента по HTTP API; полная спецификация — `GET /openapi.json`, интерактивно — `GET /docs` |",
        "| Источник | `python tools/openapi_digest.py` (генерируется из `app.openapi()`) |",
        "| Статус | approved — синхронность с кодом проверяет `tests/test_openapi_digest.py` |",
        "",
        "## 1. Аутентификация",
        "",
        "- `Authorization: Bearer <PARTNER_API_TOKEN>` — глобальный административный токен;",
        "- tenant-токены из `PARTNER_TENANT_TOKENS` — роли `director`, `participant`, `lawyer`;",
        "- вебхуки коннекторов аутентифицируются HMAC-подписью платформы, а не Bearer;",
        "- без токена открыты `/healthz`, `/v1/healthz`, `/v1/metrics`, `/v1/license`.",
        "",
        "`/dashboard` — статическая страница; данные она запрашивает у `/v1`",
        "с токеном из `sessionStorage`.",
        "",
        "## 2. Эндпоинты",
        "",
    ]

    groups = _endpoint_groups(spec)
    for tag in sorted(
        groups,
        key=lambda name: (
            TAG_ORDER.index(name) if name in TAG_ORDER else len(TAG_ORDER),
            name,
        ),
    ):
        title = TAG_TITLES.get(tag, tag)
        lines.extend(
            [
                f"### {title}",
                "",
                "| Метод | Путь | Назначение |",
                "|---|---|---|",
            ]
        )
        for method, path, description in sorted(
            groups[tag], key=lambda item: (item[1], METHOD_ORDER.index(item[0].lower()))
        ):
            lines.append(f"| {method} | `{path}` | {description} |")
        lines.append("")

    schemas = spec.get("components", {}).get("schemas", {})
    lines.extend(
        [
            "## 3. Схемы запросов",
            "",
            "| Схема | Обязательные поля | Все поля |",
            "|---|---|---|",
        ]
    )
    for name in _request_schemas(spec):
        schema = schemas.get(name, {})
        required = ", ".join(f"`{field}`" for field in schema.get("required", [])) or "—"
        properties = ", ".join(f"`{field}`" for field in schema.get("properties", {})) or "—"
        lines.append(f"| `{name}` | {required} | {properties} |")
    lines.extend(
        [
            "",
            "## 4. Ошибки",
            "",
            'Ошибки возвращаются как `{ "detail": { "code": "...", "detail": "..." } }`.',
            "Основные коды: `no_session`, `no_policy`, `day_other_project`,",
            "`cross_tenant_denied`, `participant_scope_denied`, `day_project_conflict`,",
            "`legal_state_busy`, `manual_only_policy_not_supported`, `version_backdated`,",
            "`device_signature_required`, `license_invalid`, `license_expired`.",
            "",
            "Коды и HTTP-статусы совпадают с `docs/03-architecture/overview.md` §6",
            "и `docs/07-ops/installation.md`.",
            "",
        ]
    )
    return "\n".join(lines)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DOC_PATH)
    parser.add_argument("--check", action="store_true", help="не писать, проверить актуальность")
    args = parser.parse_args(argv)

    from partner_server.main import app

    expected = render_digest(app.openapi())
    if args.check:
        actual = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if actual != expected:
            print(
                f"OpenAPI-выжимка устарела: {args.out}; "
                "запустите python tools/openapi_digest.py",
                file=sys.stderr,
            )
            return 1
        print(f"OpenAPI-выжимка актуальна: {args.out}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(expected, encoding="utf-8")
    print(f"записано: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
