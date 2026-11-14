# Установка и эксплуатация (MVP)

| Поле | Значение |
|---|---|
| Версия | 1.1 (M8: compose, бэкапы, метрики) |
| Дата | 2026-11-13 |
| Аудитория | администратор клиента, директор |
| Связанные | quickstart, runbook.md, ADR-0018 (подписка), ADR-0020 (бэкапы) |

## 1. Требования

| Компонент | Минимум |
|---|---|
| ОС | Linux x86-64 (проверено: Debian 12, Ubuntu 22.04+) |
| Docker / Compose | Docker Engine 24+, Compose v2 |
| Ресурсы VM | 2 vCPU / 4 GB RAM / 40 GB SSD |
| Сеть | доступ к git-платформе (GitHub или Forgejo/Gitea), HTTPS наружу для дайджестов |
| Python (для CLI) | 3.10+ |

## 2. Установка сервера (Docker Compose, M8.1)

Поставка — `deploy/`: docker-compose.yml (db + migrate + server + backup,
разовые джобы — dayclose) и скрипты scripts/.

```bash
mkdir -p /opt/partner && cd /opt/partner
# поставка пакета подписки (ADR-0018): распаковать каталог deploy/
cp deploy/.env.example deploy/.env && $EDITOR deploy/.env   # только секреты
export PARTNER_IMAGE=partner:1.2.0
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps   # migrate = Exit 0, server = healthy
```

Схема БД накатывается сервисом `migrate` (alembic upgrade head) при каждом
`up`/обновлении; сервер стартует только после успешной миграции.

Коннекторы v1.1: задайте `PARTNER_CONNECTOR_SECRET` (общий) или
`PARTNER_CONNECTOR_SECRETS` (JSON `project_id → секрет`) — без секрета
вебхуки возвращают 503. URL вебхука: `https://<host>/v1/connectors/github/webhook`
(аналогично `forgejo`/`gitea`).

Мультитенантность v1.2: `PARTNER_TENANT_TOKENS` — JSON-карта
`{"<token>": "<tenant_id>"}` (или `{"<token>": {"tenant_id": "...",
"role": "director", "participant_id": "..."}}`). Токен из карты видит только
проекты, дни, споры, уведомления, аудит и снапшоты своего tenant;
`PARTNER_API_TOKEN` остается глобальным административным токеном. Без карты
поведение прежнее: один токен на установку.

Роли (T-06): `director` (по умолчанию) — сводка, флаги, споры, аудит,
снапшоты, чтение legal; `participant` (обязателен `participant_id`) — только
свои сессии, дни, статус, устройства и уведомления, чужие → 403
`participant_scope_denied`; `lawyer` — legal read, гейты G-01…G-08, enable и
чтение снапшотов, без данных времени. Отказы пишутся в аудит как
`access.denied`. Роль `participant` без `participant_id` закрыта (fail closed).

Проверка:

```bash
curl -fsS http://localhost:8080/healthz     # {"status":"ok"}
curl -fsS http://localhost:8080/v1/metrics  # метрики (§6)
docker compose logs server | grep dayclose  # планировщик закрытия дня
```

Кабинет директора: `http://localhost:8080/dashboard` (v1.3). Введите
`PARTNER_API_TOKEN` или tenant-токен роли `director`/`admin`; при пустом
токене сервер работает в демо-режиме и доступна кнопка «Демо без токена».
Participant-токен видит только свои данные и не откроет сводку/флаги/споры.

Восстановление из бэкапа отрепетировать сразу после установки:
`docker compose exec backup /srv/scripts/restore_check.sh` (runbook §5.1).

## 3. Установка CLI

Пакет `partner-tracker` поставляется одним из способов:

1. **Приватный PyPI-индекс** (рекомендуется для организаций):
   ```bash
   pip install --index-url https://<user>:<token>@pypi.example/partner/simple partner-tracker
   ```
2. **Wheel-артефакт публичного GitHub Release**
   (https://github.com/biznesemarket/partner). Прямая ссылка
   `releases/download/...` работает без токена; в релизе лежит
   `dist-manifest.json` с `sha256` каждого артефакта.
   ```bash
   curl -fLO https://github.com/biznesemarket/partner/releases/download/v1.3.0/partner_tracker-1.3.0-py3-none-any.whl
   echo "<sha256>  partner_tracker-1.3.0-py3-none-any.whl" | sha256sum -c -
   pip install partner_tracker-1.3.0-py3-none-any.whl
   ```
   Из checkout репозитория вендора: `python tools/partner_dist.py resolve
   --version latest --download dist-release` (токен не нужен). Для
   приватного зеркала с API URL asset задайте `--token`/`GH_TOKEN`
   (fine-grained PAT, Contents: Read-only).
3. **Локальная разработка продукта**: `pip install -e ".[dev,server]"`.

```bash
partner init                  # мастер настройки (docs/00-start/quickstart.md)
```

CI клиента (`repo-pack/workflows/partner-policy-check.yml`) берет поставку
только из секретов (публичный PyPI не используется — fail closed):

| Секрет | Значение |
|---|---|
| `PARTNER_TRACKER_WHEEL` | публичный browser URL wheel из release (или API URL приватного зеркала) |
| `PARTNER_TRACKER_TOKEN` | только для приватного API URL/зеркала: fine-grained PAT, Contents: Read-only |
| `PARTNER_TRACKER_SHA256` | sha256 wheel из `dist-manifest.json` (проверка целостности) |
| `PARTNER_TRACKER_INDEX_URL` | альтернатива wheel: приватный индекс с credentials в URL |

Команды настройки клиентского репозитория печатает
`python tools/partner_dist.py resolve --version latest --target-repo <owner/repo>`.
Без токена печатаются browser URL и `PARTNER_TRACKER_SHA256` (токен не
требуется); с `--token` — API URL и команда `PARTNER_TRACKER_TOKEN` для
приватного зеркала. Токен не должен быть широким PAT владельца вендора.

Прототип может работать без сервера: CLI держит SQLite в `./data/partner.sqlite3`
(схема `apps/partner_cli/schema.sql`), это режим демо и оценки.
Рабочий контур — сервер + PostgreSQL.

## 4. Обновления и миграции (M8.2)

Порядок обновления без потери дней (скрипт `deploy/scripts/upgrade.sh`
выполняет шаги 2–5 автоматически):

1. `deploy/scripts/upgrade.sh <новый тег>` — бэкап → alembic upgrade →
   перезапуск → проверка /healthz.
2. Откат при неудаче — `deploy/scripts/rollback.sh <старый тег>`;
   данные БД заменяются из дампа только по явному решению (⚠ runbook §5.2).
3. Инвариант: закрытые дни (day_records) неизменны между версиями —
   миграции только добавляют таблицы/колонки (0001…0004), начисления
   не пересчитываются задним числом (V-02/V-04).

Обновление политики клиента: новая версия policy.yaml через PR
(валидация workflow `partner-policy-check.yml`), политику версонирует
мастер/CLI; заднее число запрещено.

## 5. Резервное копирование (M8.3, ADR-0020)

| Объект | Метод | Периодичность |
|---|---|---|
| PostgreSQL | сервис `backup`: pg_dump -Fc + проверка дампа | ежедневно, `PARTNER_BACKUP_AT` (02:00) |
| policy.yaml и AGENTS.md | сам репозиторий проекта | при каждом коммите |
| legal.yaml (состояние юрмодуля) | администратор (не в git, legal-module.md §6) | при изменении |
| git-якоря (ledger/) | сам git | при каждом якоре |
| Ключи и лицензия | менеджер секретов клиента | при изменении |

Хранение: `PARTNER_BACKUP_KEEP_DAYS` дней (дефолт 14); каждый дамп
проверяется сразу после снятия (`gzip -t` + `pg_restore --list`).

**Офсайт-копия обязательна** (замена ADR-0013, ADR-0020 §3): минимум одна
копия дампов вне сервера. Способ фиксирует клиент (S3-совместимое
хранилище / git-LFS / скачивание администратором) в журнале эксплуатации.

Разовый дамп и репетиция восстановления:

```bash
docker compose exec backup /srv/scripts/backup.sh once    # дамп с проверкой
docker compose exec backup /srv/scripts/restore_check.sh  # репетиция (runbook §5.1)
```

Репетиция восстановления выполняется после установки, ежеквартально и
входит в upgrade.sh перед каждой миграцией.

## 6. Мониторинг (M8.4)

Метрики отдаются на `GET /v1/metrics` (формат Prometheus text, без
персональных данных — только агрегаты; скрейп каждые 60 с без токена).

| Метрика | Норма | Алерт (runbook §3) |
|---|---|---|
| `partner_heartbeats_last_hour` | > 0 в рабочие часы | stalled > 30 мин |
| `partner_day_close_delay_seconds` | < 900 | опоздание закрытия дня |
| `partner_days_open_overdue` | 0 | > 0 — дни старше грейса |
| `partner_digest_pending` / `partner_digest_failed` | 0 | сбои доставки |
| `partner_days_closed_total`, `partner_heartbeats_total`, `partner_intervals_total` | рост | счетчики |
| `partner_disputes_open` | информационная | директору |

Примеры алерт-правил и чек-лист дежурного — runbook §6 и §3.

## 7. Runbook: типовые ситуации

Полный runbook — `docs/07-ops/runbook.md`; кратко:

| Ситуация | Действие |
|---|---|
| day_close не сработал | `docker compose run --rm dayclose` (идемпотентно); точечный день — `--date <D>` |
| Сбой обновления | `deploy/scripts/rollback.sh <тег>` (runbook §5.2) |
| Утечка токена | ротация `PARTNER_API_TOKEN` + `docker compose up -d server` (runbook §4.7) |
| Спор о дне | не править БД руками; dispute → resolve → adjustment (runbook §4.4) |
| Потеря VM | восстановление из дампа + restore_check (runbook §4.6, RPO ≤ 24 ч) |
| Переоценка | новая версия valuation в policy.yaml с adr_ref; уведомление участникам |

## 8. Снятие системы

Экспорт всех данных: `partner snapshot YYYY-MM` + pg_dump + CSV.
Система не блокирует экспорт своих данных клиентом — данные принадлежат
клиенту (vision: «данные клиента — у клиента»).

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 1.1 | 2026-11-13 | M8: compose-установка, миграции, бэкапы ADR-0020, метрики, скрипты |
| 1.0 | 2026-11-13 | Установка MVP, бэкапы, мониторинг, runbook |
