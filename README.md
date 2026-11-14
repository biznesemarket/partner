# Партнер

**Время работы с ИИ-агентом → деньги и доля в проекте.**
Self-hosted система ежедневного учета вклада разработчиков, которую можно продавать
любым организациям. Первый клиент — ООО «ФОНИИ» (tenant #1).

Продукт распространяется по закрытой подписке (ADR-0018). Код открыт только в
периметре клиента по лицензионному ключу.

## Что делает

- Директор один раз отвечает на вопросы мастера `partner init`: оценка проекта,
  ставка часа, дневной кап, коэффициент автономного времени агента, пул участников.
  Юридические данные можно пропустить и заполнить позже.
- Правила ложатся в репозиторий проекта: `policy.yaml` + блок в `AGENTS.md`,
  который ИИ-агент читает первым.
- Разработчик работает с ИИ-агентом в своей ветке; агент-кит фиксирует сессии
  и активность (heartbeat раз в 30 секунд).
- В 00:00 по Москве (пояс — переменная политики) день закрывается автоматически:
  считаются оплачиваемые часы по классам времени, применяются коэффициенты и капы,
  начисляются рубли и доля.
- Утром разработчик получает дайджест: сколько получено за день и за весь период,
  в процентах и рублях, какие капы и флаги применены.
- Директор видит сводку, флаги аномалий и может скорректировать день
  в окне оспаривания (3 суток).

## Ключевые цифры (дефолты политики tenant #1)

| Параметр | Значение | Кто задает |
|---|---|---|
| Оценка проекта | 10 000 000 ₽ | директор при настройке |
| Ставка часа работы с ИИ | 500 ₽/ч | директор, версионируется |
| Дневной кап оплачиваемых часов | 12 ч | директор при настройке |
| Коэффициент автономного времени агента | 0,2 (реком. 0,2–0,3) | директор при настройке |
| Кап автономного класса | 2,0 оплач. ч/сутки | директор при настройке |
| Пул участников | 30% от оценки | директор при настройке |
| Закрытие дня | 00:00 Europe/Moscow | политика проекта |

## Компоненты

| Компонент | Назначение |
|---|---|
| `partner-server` | API, БД (PostgreSQL), планировщик, расчет дней, дайджесты |
| `partner-agent-kit` | плагин IDE + MCP-сервер: сессии, heartbeat, отчет агента |
| `partner-connector-*` | GitHub / Forgejo / Gitea: ветки, PR, вебхуки |
| `repo-pack` | ставится в репозиторий проекта клиента: AGENTS.md, policy.yaml, workflows |
| `partner-cli` | `init`, `status`, `day`, `explain`, `snapshot` |

## Документация

| Раздел | О чем |
|---|---|
| [00-start](docs/00-start/) | Быстрый старт, план итераций, карта переноса прежней версии |
| [01-product](docs/01-product/) | Видение, глоссарий |
| [02-policy](docs/02-policy/) | ★ Ядро: модель учета времени, экономика долей, схема политики |
| [03-architecture](docs/03-architecture/) | Архитектура, данные, мастер настройки, ADR |
| [04-security](docs/04-security/) | Модель угроз и антифрод (M5: детекты реализованы) |
| [06-legal](docs/06-legal/) | Опциональный юридический модуль |
| [07-ops](docs/07-ops/) | Установка и эксплуатация |
| [08-guides](docs/08-guides/) | Дайджест и CLI для директора и участника |
| [09-delivery](docs/09-delivery/) | Готовые инструкции DOCX/PDF для поставки клиенту и вендору |
| [repo-pack](repo-pack/) | Пакет, который ставится в репозиторий проекта клиента |

## Быстрый старт (скелет)

В репозитории лежит рабочий скелет CLI: мастер настройки `partner init`,
калькулятор дня с 12 приемочными тестами и валидатор политики.

```bash
pip install -e ".[dev,server]"
pytest                       # весь набор: T-01..T-12 + сервер, антифрод, дайджесты
ruff check .                 # линтер кода (CI: Linux + Windows)
python tools/policy_lint.py docs/02-policy/policy.example.yaml
partner init --demo          # мастер настройки, демо-режим без вопросов
```

## Запуск сервера (M4–M7)

FastAPI-сервер учета времени: идемпотентный heartbeat, сборка и закрытие
дня через golden-калькулятор, объяснение начислений, месячные сводки,
антифрод, дневной/месячный дайджесты, спор дня, юридический модуль
(снапшоты долей, гейты, экспорт юристу).

```bash
pip install -e .[server]
export PARTNER_DB_URL="sqlite:///./data/partner.db"   # или PostgreSQL-DSN
export PARTNER_SCHEDULER=1                            # закрытие дня 00:00 + дайджесты
# каналы доставки (M6.3): agent_chat встроен; опционально:
export PARTNER_WEBHOOK_URL="https://chat.example.local/hook"  # мессенджеры
export PARTNER_SMTP_HOST="smtp.example.local"                 # email-fallback
export PARTNER_DISPUTE_DAYS=3                          # окно спора дня
export PARTNER_LEGAL_FILE="legal.yaml"                 # состояние юрмодуля (M7)
# лицензия подписки (M9.1; в demo не требуется):
export PARTNER_LICENSE_KEY="PARTNER.fonii.basic.2027-11-13.<подпись>"
export PARTNER_LICENSE_SECRET="секрет-поставки"        # выдается вне канала ключа
export PARTNER_LICENSE_REQUIRED=1                      # боевой контур
# подпись устройств (threat-model §4, опционально):
export PARTNER_REQUIRE_DEVICE_SIGN=0
uvicorn apps.partner_server.main:app --port 8000      # OpenAPI: /docs
```

Эндпоинты: `POST /v1/sessions`, `POST /v1/heartbeats`,
`GET|POST /v1/days/{date}[/close|/explain|/disputes]`, `GET /v1/status/*`,
`GET /v1/digest/day/{date}`, `GET /v1/digest/month/{month}`,
`POST /v1/disputes/{id}/resolve`, `GET /v1/disputes`,
`GET /v1/notifications`, `POST /v1/policies`, `POST /v1/rate-versions`,
`GET /v1/audit`, `GET /v1/legal[/gates/{id}|/enable]`,
`POST /v1/snapshots/{month}`, `GET /v1/snapshots[/{id}]`,
`POST /v1/legal/export`, `GET /v1/metrics` (M8, Prometheus),
`GET /v1/license`, `POST /v1/devices` (M9), `GET /healthz`.
Миграции: `alembic upgrade head`
(см. `alembic.ini`). Антифрод M5: night/synthetic/replay/clock_skew/late
детекты + изоляция tenants (403) + append-only аудит.

Спор дня (M6.4) из CLI — через сервер:

```bash
export PARTNER_SERVER_URL=http://localhost:8000
partner dispute 2026-11-12 --by dev1 --reason "агент был выключен" --interval e3
partner dispute-decide dsp-… --by директор --decision approved --delta-rub -100
partner digest day 2026-11-12 --participant dev1
```

Дайджест дня доставляется автоматически после закрытия дня
(agent_chat/email/webhook с повтором при недоставке); месячный — 1-го
числа за предыдущий месяц.

### Юридический модуль (M7, опциональный)

Продукт полностью работает при `legal.enabled: false` (legal-module.md §1):
учет, дайджесты и споры не создают юридических обещаний. Включение —
только после снятия юристом гейтов G-01…G-08:

```bash
export PARTNER_SERVER_URL=http://localhost:8000
partner legal-gates                                    # отчет по гейтам
cp docs/06-legal/legal.state.example.yaml legal.yaml   # чистое состояние
# … юрист снимает гейты (основание ≥5 символов) и включает модуль:
partner legal-gate G-01 --status done --evidence "договор №1 от 2026-11-13" --by юрист
partner legal-enable --by юрист

# месячный снапшот долей (иммутабелен, sha256 + git-якорь):
partner snapshot-close 2026-11 --by директор

# пакет юристу (legal-module.md §7):
partner export-legal --to lawyer/ --by директор
#   → policy.json/md, snapshots/<месяц>.md (подписываемые листы),
#     gates.md, questions-to-lawyer.md, templates/*.md (M7.4), MANIFEST.txt
```

## Поставка Docker Compose (M8)

```bash
cd deploy
cp .env.example .env && $EDITOR .env   # только секреты
docker compose up -d                   # db → migrate (alembic) → server + backup
curl -fsS http://localhost:8080/v1/metrics
# обновление: ../scripts/upgrade.sh <тег>; откат: ../scripts/rollback.sh <тег>
# бэкап: docker compose exec backup /srv/scripts/backup.sh once
# репетиция восстановления: docker compose exec backup /srv/scripts/restore_check.sh
```

Подробности — [docs/07-ops/installation.md](docs/07-ops/installation.md),
эксплуатация и алерты — [docs/07-ops/runbook.md](docs/07-ops/runbook.md),
политика бэкапов — [ADR-0020](docs/03-architecture/adr/ADR-0020-backup-policy.md).

## Статус · v1.0.0

Все итерации M0–M9 выполнены: CLI и калькулятор (M1–M3), сервер (M4),
антифрод и аудит (M5), дайджесты и спор дня (M6), юридический модуль (M7),
поставка и эксплуатация (M8), релиз — лицензирование подписки, подпись
устройств, docs-сайт, приемка (M9; **131 тест**, акт приемки —
docs/05-process/release-acceptance.md). Осталась организационная часть
DoD: пилотная установка у первого клиента.
План — [docs/05-process/development-plan.md](docs/05-process/development-plan.md).

## Лицензия

Проприетарная, закрытая подписка. Все права защищены. См. [LICENSE](LICENSE).
