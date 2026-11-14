# История изменений

Формат — Keep a Changelog, версии — SemVer.

## [1.2.0] — 2026-11-14

### Добавлено — эксплуатация и доверие (issues #4–#7)

- **Tenant/RBAC (issue #4)**: `PARTNER_TENANT_TOKENS` привязывает Bearer-токен
  к tenant; `DayRecord.project_id` и project-scoped `day_id` разделяют дни
  проектов; списки статусов, споров, уведомлений, аудита и снапшотов
  фильтруются по tenant/project; миграция `0007_tenant_rbac`, тесты изоляции.
- **Месячный дайджест (T-02)**: остаток пула и дни считаются только по
  проекту, а не по всем `day_records`.
- **Инвариант закрытого дня (issue #7, ADR-0021)**: `close_day` не
  перезаписывает принятый `day_record`; повтор идемпотентен, поздние события
  создают append-only `adjustment` на дельту, повторная компенсация не
  дублируется; дайджест показывает пересчет и компенсацию.
- **PostgreSQL и Docker в CI (issue #5)**: job `postgres-smoke` с
  `postgres:16` — `alembic upgrade head`/`check`, seed, uvicorn, `/healthz`,
  закрытие и чтение дня; job `docker-build` — compose config и сборка образа.
  Добавлен идемпотентный `python -m partner_server.seed`.
- **Поставка `partner-tracker` (issue #6)**: release-workflow собирает
  wheel/sdist и создает GitHub Release; CI `package-smoke` ставит wheel в
  чистое окружение и прогоняет `partner day --demo` + `policy_lint` на
  чистом репозитории клиента; repo-pack workflow поддерживает
  `PARTNER_TRACKER_WHEEL`/`PARTNER_TRACKER_INDEX_URL`; `jsonschema` и
  `tzdata` добавлены в зависимости пакета; версия 1.2.0.

## [1.1.0] — 2026-11-14

### Добавлено — сквозной учет (issues #1–#3)

- **`partner-agent-kit`** (`apps/partner_agent_kit`): CLI/daemon сессий,
  heartbeat 30 с, разрез интервала по локальной полуночи пояса проекта,
  классификация `active_human/supervised_agent/autonomous_agent/idle`,
  офлайн-буфер JSONL с отправкой в порядке `seq`, HMAC-подпись устройства,
  MCP-сервер stdio (6 инструментов, `confirm=true` для старта и спора),
  VS Code-обертка. Общая строка подписи — `partner_cli.signing`.
- **Коннекторы GitHub/Forgejo/Gitea**: вебхуки `push`, `pull_request`,
  `issues` с HMAC-подписью и дедупликацией delivery; таблицы
  `connector_events` и `repo_facts` (миграция `0006_connectors`); привязка
  пустых `task_ref`/`branch` сессий по ветке `dev/<логин>/<тема>`;
  эндпоинты `/v1/connectors/*`; backfill через `partner-connector sync-repo`.
- **`no_output`**: флаг при >3 оплач. ч без коммитов, PR-диффов и
  `signals.tool_calls`, привязанных к ветке/задаче сессий участника.
- **Тесты**: 156 (25 новых): подпись, replay/duplicate seq, офлайн-буфер,
  полночь, MCP, вебхуки, дедупликация, привязка сессий, no_output,
  нормализация API GitHub/Forgejo.

### Документация

- `agent-kit.md` и `connectors.md` переведены в approved и описывают
  реализацию v1.1; data-model дополнена двумя таблицами и API; backlog
  B-01/B-02/B-04 закрыты.

## [Незапланировано]

### Транспорт agent_chat (B-03, 2026-11-14)

- `notify.py`: канал `agent_chat` всегда пишет в журнал; при
  `PARTNER_AGENT_CHAT_URL` отправляет JSON в адаптер чата агента
  (`PARTNER_AGENT_CHAT_TOKEN` — Bearer), ошибка адаптера уходит в fallback.
- `partner-agent-kit`: MCP-инструмент `partner_inbox` отдает очередь
  уведомлений участника (`/v1/notifications`), `PartnerClient.list_notifications`.
- Документация: overview, agent-kit, runbook, backlog.

### Надёжность legal.yaml и демо-профиль (T-04, T-07, 2026-11-14)

- **legal.yaml**: межпроцессная блокировка `_state_lock` вокруг
  read-modify-write и атомарная замена через `os.replace`; читатель не
  видит частичную запись, занятость дает `legal_state_busy` (409).
- **Демо-профиль**: `WizardAnswers.demo()` больше не содержит ФИО
  директора — «ООО «Демо»» / «Директор (демо)».

### Веб-дашборд директора (issue #10, 2026-11-14)

- **`/dashboard`**: самодостаточная HTML-страница без сборки и внешних
  зависимостей; токен только в `sessionStorage`, данные — через `/v1`.
  Сводка, флаги, споры (одобрение/отклонение), снапшоты (закрепление
  месяца), экспорт CSV/JSON.
- **`GET /v1/flags`**: антифрод/кап-флаги месяца с исходной и эффективной
  суммой дня, tenant/project-scoped.
- **`GET /v1/status/summary`**: добавлены `project_id`,
  `adjustments_delta_cents` и `effective_amount_rub`; исходные поля
  сохранены для совместимости.

### Поставка: приватный wheel и fail-closed CI (2026-11-14)

- **repo-pack**: workflow больше не ставит одноименный пакет с публичного
  PyPI — без `PARTNER_TRACKER_*` падает. Для приватного GitHub Release
  asset обязательны `PARTNER_TRACKER_WHEEL` (API URL) и
  `PARTNER_TRACKER_TOKEN` (fine-grained PAT, Contents: Read-only);
  `PARTNER_TRACKER_SHA256` проверяет целостность wheel.
- **Инструменты**: `tools/partner_dist.py resolve` находит wheel, sha256 и
  печатает команды `gh secret set`; `manifest` собирает
  `dist-manifest.json`; release-workflow прикладывает манифест к релизу.
- **CI**: `package-smoke` скачивает приватный release asset по
  `GITHUB_TOKEN`, ставит его в чистое окружение и прогоняет
  `partner day --demo`.

### Подготовка к публикации на GitHub (2026-11-14)

- **Кроссплатформенность**: тесты и инструменты больше не падают на Windows
  (проверка прав 0600 только на POSIX; PATH наследуется в alembic-тесте;
  stdout/stderr переключаются в `errors="replace"` для cp1251-консоли).
- **CI**: матрица Linux + Windows, шаг `ruff check .`, проверка обоих
  примеров политики; артефакт docs-сайта на каждую ОС.
- **Поставка**: Docker-образ копирует LICENSE; `deploy/legal` монтируется
  read-write (гейты и включение юрмодуля пишут состояние через API);
  `release.sh` собирает образ и проверяет compose-конфиг из `deploy/`;
  `upgrade.sh` использует `docker compose run --rm migrate` и код возврата.
- **Репозиторий**: CONTRIBUTING.md, SECURITY.md, PR-шаблон, dependabot,
  .editorconfig, .gitattributes; .gitignore расширен под артефакты и секреты.
- **Документация**: актуализированы статусы docs-plan.md и
  documentation-roadmap.md (D4/D6 — частично, D7 — на ревью), убраны
  дубли в реестре документов; текущий набор тестов — 131.

### Исправлено (автономный аудит кода, 2026-11-13) — 131 тест

- **Безопасность (P0)**: все финансовые GET-эндпоинты (дни, статусы,
  дайджесты, аудит, уведомления, споры, снапшоты) теперь требуют
  Bearer-токен при заданном PARTNER_API_TOKEN — раньше читались без
  аутентификации; сравнение токена переведено на
  `hmac.compare_digest` (timing-безопасно). Публичными остаются только
  задокументированные `/v1/metrics` и `/v1/license`.
- **R8 (P1)**: `cash_share_pct` политики теперь применяется при закрытии
  дня и в CLI-прототипе — раньше `cash_cents` всегда был 0, весь день
  попадал в `equity_cents`.
- **API (P1)**: `GET /v1/status/summary` снова достижим — статический
  маршрут перебивался динамическим `/status/{participant_id}` (всегда 404).
- **Кэш соединений (P1)**: `get_db()` переиспользует дефолтный engine —
  раньше создавался новый пул соединений на каждый запрос.
- **Окно дня (P2)**: выборка интервалов ограничена с обеих сторон
  (`end_utc > полночь(D)`) — compute_day больше не читает всю историю
  участника; окно строится от локальной полуночи проекта (устранен сдвиг
  для поясов с отрицательным смещением).
- **Инвариант закрытых дней (P1)**: `partner day`/storage прототипа
  отклоняют перезапись закрытой записи с другим содержимым (только
  adjustments, как на сервере).
- **Метрики (P2)**: `days_open_overdue` — дедупликация (participant, дата)
  вместо N+1 запроса на интервал.
- **Поставка (P1)**: docker compose и `.env.example` пробрасывают
  `PARTNER_LICENSE_*` и `PARTNER_REQUIRE_DEVICE_SIGN` — лицензирование M9
  теперь включаемо в compose-установке (раньше всегда demo-режим).
- Доли месяца суммируются в Decimal (calculator-spec §2); месяц по
  умолчанию берется по поясу проекта, а не UTC; новые регрессионные
  тесты аудита — `tests/test_audit_fixes.py` (12 шт.).

### Планируется (после v1.0.0)

- Установка у пилотного клиента (ФОНИИ) и организационное закрытие DoD M9
- Docs-сайт на публичном домене (артефакт CI готов, D9)

## [1.0.0] — 2026-11-13

### Добавлено (M9 · Релиз v1.0)

- Лицензирование закрытой подписки (M9.1, ADR-0018): `licensing.py` —
  ключ PARTNER.<tenant>.<edition>.<дата>.<HMAC> (выдача:
  tools/license_issue.py), состояния demo/invalid/active/grace/expired,
  грейс-период PARTNER_LICENSE_GRACE_DAYS; после грейса новые закрытия
  дня блокируются (402 license_expired), чтение/экспорт/снятие системы
  не блокируются. GET /v1/license, метрика partner_license_days_left,
  состояние в логе при старте. Конфиг: PARTNER_LICENSE_KEY/SECRET/
  REQUIRED/GRACE_DAYS.
- Подпись устройств источников (threat-model §4 → M9): таблица devices
  (миграция 0005_m9), POST /v1/devices (секрет выдается один раз),
  POST /v1/devices/{id}/revoke, GET /v1/devices; HMAC-SHA256 подпись
  heartbeat (X-Partner-Device/X-Partner-Sign), PARTNER_REQUIRE_DEVICE_SIGN=1
  отклоняет неподписанные батчи (422 device_signature_required),
  инциденты unsigned_heartbeat / device_invalid / device_signature_invalid
  в аудит. threat-model → 2.1.
- Docs-сайт (M9.3 / D9): tools/build_docs_site.py — сборка из docs/
  без ручной правки, публикуется только approved (статус из документа
  или реестра docs-plan §5); шаг в CI (артефакт docs-site).
- Документы D9: docs/10-gtm/positioning.md (позиционирование и
  конкурентное поле), docs/10-gtm/licensing-pricing.md (тарифы, SLA,
  порядок выдачи ключей) — approved.
- Сборка релиза (M9.2): deploy/scripts/release.sh — образ с тегом,
  docker save, SHA256SUMS, манифест RELEASE-<ver>.md;
  docs/07-ops/migration-guide.md (пути с 0.3/0.4–0.7/0.8 на 1.0.0).
- Приемка (M9.4): tools/release_check.py — автоматический прогон
  чек-листа development-plan §6; акт docs/05-process/release-acceptance.md
  (автоматическая часть ПРОЙДЕНА).

### Изменено

- CI: исправлен триггер push (branches: [main]), добавлен шаг сборки
  docs-сайта с артефактом.
- dev-зависимость markdown-it-py (генератор сайта).
- Версия 1.0.0.

### Тесты

- tests/test_license.py — 9 (ключ, грейс, блокировка закрытия 402,
  чтение доступно, подделка, REQUIRED без ключа).
- tests/test_devices.py — 6 (регистрация, подпись, подделка/отзыв,
  обязательный режим).
- tests/test_site.py — 6 (approved-фильтр, HTML без сырых таблиц).
  Всего в репозитории 131 тест (111 + 12 аудита + 8 выдачи лицензий).

## [0.8.0] — 2026-11-13

### Добавлено (M8 · Поставка и эксплуатация)

- `deploy/` (M8.1): docker-compose.yml — PostgreSQL 16 (не публикуется
  наружу), разовый `migrate` (alembic upgrade head; сервер стартует только
  после успешной миграции), `server` (healthcheck, планировщик закрытия
  дня), `dayclose` (tools-профиль, `python -m partner_server.dayclose_cli
  [--date D]`), `backup` (ежедневный дамп); Dockerfile (python:3.12-slim,
  postgresql-client); `.env.example` — секреты только через .env.
- Обновления (M8.2): `deploy/scripts/upgrade.sh` (бэкап → alembic upgrade →
  перезапуск → /healthz; при неудаче прерывание до перезапуска),
  `rollback.sh` (откат образа, восстановление БД — только явно).
  Миграция `0004_m8` (day_records.closed_at), полный цикл
  upgrade→downgrade→upgrade проверен, `alembic check` чист.
- Бэкапы (M8.3, ADR-0020): `deploy/scripts/backup.sh` — ежедневный
  pg_dump -Fc в `PARTNER_BACKUP_AT`, проверка каждого дампа (gzip -t +
  pg_restore --list), retention 14 дней; `restore_check.sh` — репетиция
  восстановления во временную БД со сверкой с боевой (версия схемы,
  таблицы, day_records, sha256). ADR-0020 заменяет ADR-0013
  (офсайт-копия обязательна).
- Мониторинг (M8.4): `GET /v1/metrics` — Prometheus text, без токена,
  только агрегаты: heartbeats_total/last_hour, intervals, days_closed,
  day_close_delay_seconds, days_open_overdue, digest_pending/failed,
  disputes_open. Алерт-правила — runbook §6.
- Runbook (M8.5): `docs/07-ops/runbook.md` 1.0 — точки здоровья, пороги
  алертов, типовые сбои (закрытие дня, доставка, потеря данных RPO ≤ 24 ч,
  утечка токена), процедуры (репетиция восстановления, откат).
- `installation.md` 1.1 (approved): compose-установка, порядок обновлений,
  бэкапы, метрики.

### Изменено

- models.py: DayRecord.closed_at — момент фактического закрытия (метрика
  задержки закрытия дня; повторное закрытие не перезаписывает первую).
- README: раздел «Поставка Docker Compose (M8)».

### Тесты

- tests/test_ops.py — 9 тестов (O-01…O-08): метрики (формат Prometheus,
  offset-безопасный подсчет heartbeats), closed_at при закрытии и
  идемпотентность, days_open_overdue, споры/доставка, разовая джоба
  dayclose_cli, /v1/metrics без токена и без персональных данных.
  Всего в репозитории 90 тестов.

## [0.7.0] — 2026-11-13

### Добавлено (M7 · Юридический модуль)

- `apps/partner_server/legal.py` — опциональный юридический модуль
  (legal-module.md): снапшоты долей (M7.1), гейты G-01…G-08 (M7.3),
  экспорт юристу (M7.2), шаблоны фиксации без логики (M7.4). Принцип §1:
  продукт полностью работает при `legal.enabled: false` (покрыто тестом).
- Снапшоты долей (M7.1): `partner snapshot-close <YYYY-MM>` /
  `POST /v1/snapshots/{month}` — месячный итог по закрытым day_records:
  вклад ₽, cash/equity, расчетные доли бп всех участников tenant,
  версии политики; sha256 по canonical JSON payload, git-якорь
  (`git rev-parse HEAD`). Иммутабельность: повтор — duplicate при том же
  sha; изменение месяца — `snapshot_conflict` (422). Подписываемый лист
  снапшота (текст с местом подписи) — `GET /v1/snapshots/{id}` (поле
  text) и пакет юристу.
- Гейты (M7.3): состояние в файле `legal.yaml`
  (`PARTNER_LEGAL_FILE`; образец — docs/06-legal/legal.state.example.yaml).
  `GET /v1/legal` — отчет; `POST /v1/legal/gates/{gate_id}` — юрист снимает
  (done требует evidence ≥5 символов) или возвращает гейт, каждый шаг —
  в аудит; `POST /v1/legal/enable` — включение модуля запрещено при
  незакрытых гейтах: 422 `legal_gates_not_cleared` с перечнем + инцидент
  в аудит.
- Экспорт юристу (M7.2): `POST /v1/legal/export` + CLI
  `partner export-legal --to lawyer/` — политика без персональных данных,
  месячные снапшоты с подписываемыми листами, gates.md, 6 вопросов юристу,
  шаблоны M7.4 (договор участников, опционное соглашение, заём, чек-лист
  создания ООО — редакционные, без логики), MANIFEST.txt с
  `bundle_sha256` (sha256 canonical JSON ядра пакета).
- CLI: `partner legal-gates`, `legal-gate`, `legal-enable`,
  `snapshot-close`, `export-legal` (через PARTNER_SERVER_URL).
- Миграция `0003_m7`: колонка snapshots.payload_json (идемпотентно:
  на свежих БД 0001 уже создает колонку; offline --sql для PG не ломается).
  Downgrade 0001 теперь идемпотентен (checkfirst) — полный цикл
  upgrade→downgrade base→upgrade проверен.
- Конфиг: `PARTNER_LEGAL_FILE` (путь к состоянию юрмодуля).

### Изменено

- models.py: Snapshot.payload_json — содержимое снапшота (canonical JSON).
- docs/06-legal/legal-module.md v1.1: §7 дополнен реализацией M7
  (эндпоинты, CLI, снапшоты, гейты, пакет юристу).

### Тесты

- tests/test_legal.py — 15 тестов: гейты по умолчанию открыты, работа
  продукта при выключенном модуле (DoD), блокировка включения с перечнем,
  evidence-требования, включение после снятия, снапшот = суммы
  day_records + sha256 воспроизводим, duplicate/conflict/пустой месяц,
  пакет юристу (без display_name, manifest-хеш), CLI export-legal
  раскладывает файлы. Всего в репо 81.

## [0.6.0] — 2026-11-13

### Добавлено (M6 · Дайджест и уведомления)

- `apps/partner_server/digest.py` — дневной дайджест закрытого дня (часы
  по классам, сумма ₽, доля %, флаги, спорные интервалы) и месячный
  (оплачиваемые часы, вклад ₽, начисленные доли, остаток пула, pending:
  открытые споры и незакрытые дни). Числа только из БД; расхождение
  пересчета с day_record → ошибка `day_mismatch`.
- `apps/partner_server/notify.py` — очередь уведомлений и каналы доставки:
  agent_chat (основной), email (fallback, SMTP), webhook (мессенджеры,
  PARTNER_WEBHOOK_URL). Повтор при недоставке: attempts до 3, далее failed;
  ретраи при старте сервера и в планировщике.
- `apps/partner_server/disputes.py` — спор дня: разработчик помечает
  закрытый день (опционально interval_id), директор решает в окне 3 дня
  (PARTNER_DISPUTE_DAYS); approved → adjustment (append-only, ссылка на
  day_record), rejected, авто-истечение expired. Просрочка окна →
  `dispute_window_expired`.
- Модели `disputes`, `notifications` + миграция `0002_m6` (идемпотентная,
  checkfirst).
- API: `GET /v1/digest/day/{date}`, `GET /v1/digest/month/{month}`,
  `POST /v1/days/{date}/disputes`, `POST /v1/disputes/{id}/resolve`,
  `GET /v1/disputes`, `GET /v1/notifications`. `GET /v1/days/{date}`
  дополнен adjustments и эффективной суммой (amount + Σ корректировок).
- CLI: `partner dispute <date>`, `partner dispute-decide`, `partner digest
  day|month` — через PARTNER_SERVER_URL.
- Дайджест дня ставится в очередь сразу после закрытия дня (хук в
  close_day); месячные дайджесты — джобой 1-го числа за предыдущий месяц.
- Тесты `tests/test_digest.py` (18 шт., всего в репо 66): воспроизводимость
  чисел дайджеста из БД, day_mismatch, пул и pending, доставка/ретраи/failed,
  полный цикл спора с adjustment, окно 3 дня, CLI без сервера.

## [0.5.0] — 2026-11-13

### Добавлено (M5 · Безопасность и антифрод)

- `apps/partner_server/antifraud.py` — детекты по threat-model: 
  `night_autonomous_spike` (окно 22–24 ∪ 00–06 > 6 ч), `synthetic_input`
  (≥4 одинаковых интервалов ±5 с), `idle_no_signal`, `clock_skew`,
  `late_rejected` (T-08/T-09, грейс 24 ч). Флаги видны в day_records и
  explain.
- Изоляция tenants (deny by default): кросс-тенантные чтения дня/статуса
  и создание сессии → 403 `cross_tenant_denied`.
- Аудит: append-only таблица `audit_logs` + `audit.py`; инциденты
  (replay, duplicate_seq, cross_tenant_denied, rate_backdated) и действия
  над политикой/ставкой пишутся с actor, ts, старым/новым значением;
  просмотр `GET /v1/audit`.
- Эндпоинты: `POST /v1/policies` (новая иммутабельная версия политики),
  `POST /v1/rate-versions` (заднее число → 422 `version_backdated`, V-04).
- Bearer-аутентификация при заданном `PARTNER_API_TOKEN`.
- 16 красных тестов антифрода (`tests/test_antifraud.py`), общие фикстуры
  вынесены в `tests/conftest.py` (всего 48 тестов).

### Изменено

- threat-model.md 2.0: каждая атака получила статус реализации;
  подпись устройств явно перенесена в M8.

## [0.4.0] — 2026-11-13

### Добавлено (M4 · Сервер и PostgreSQL)

- `apps/partner_server/`: FastAPI + SQLAlchemy 2.0 — модели 14 таблиц,
  конфигурация через env, `/healthz`, OpenAPI `/docs`.
- Идемпотентный `POST /v1/heartbeats`: повтор event_id → duplicate,
  повтор seq в сессии → 422 duplicate_seq, пустые/обращенные интервалы
  отклоняются.
- Сборка и закрытие дня через golden-модуль `partner_cli.calc`
  (паритет T-01/T-02/T-04/T-05 через HTTP подтвержден тестами).
- `POST /v1/days/{date}/close` — идемпотентно (повтор дает тот же sha256);
  `GET /v1/days/{date}`, `/explain`; `/v1/status/{participant_id}`,
  `/v1/status/summary`.
- Джоба `dayclose.py`: закрытие предыдущего дня в 00:00 пояса проекта
  (ADR-0017) + догоняющий прогон незакрытых дней при старте.
- Миграция Alembic `0001_initial` (metadata-driven); DDL PostgreSQL
  проверен offline-генерацией, `alembic check` в тестах.
- 13 интеграционных тестов сервера (всего 32 в репозитории).

### Изменено

- pyproject: пакет `partner_server*`, extra `server` (fastapi, uvicorn,
  sqlalchemy, alembic, psycopg), `httpx` в dev.
- CI: установка `.[dev,server]` для серверных тестов.

## [0.3.0] — 2026-11-13

### Добавлено

- Мастер `partner init --demo`: 10 обязательных + опциональные юридические
  вопросы, генерация `policy.yaml` и блока `AGENTS.md` (M3, I3).
- SQLite-хранилище прототипа: `storage.py`, `schema.sql`.
- Команды CLI: `status`, `day`, `explain`, `snapshot`, `policy set`.
- `repo-pack/` для установки в репозиторий клиента: AGENTS.block.md,
  политика-пример, workflows.
- Документация: `onboarding-wizard.md`, `digest-and-cli.md`, `quickstart.md`.

## [0.2.0] — 2026-11-12

### Добавлено

- Схема политики `policy.schema.json` с разметкой `x-wizard` (M2, I2).
- Линтер политики `tools/policy_lint.py`: schema + кросс-полевые X-01…X-09
  + версионные V-01…V-08.
- Документы `policy-schema.md`, `equity-model.md`, `versioning.md`,
  `calculator-spec.md`, `policy.example.yaml`.

## [0.1.0] — 2026-11-11

### Добавлено

- Калькулятор времени `apps/partner_cli/calc.py`: классы времени,
  heartbeat 30 с, union параллельных сессий, разрез по границе суток,
  коэффициент автономии 0.2, капы, копеечная арифметика (M1, I1).
- 19 golden-тестов по сценариям T-01…T-12 (`tests/test_calc.py`).
- Документация ядра: `time-accounting.md` с 12 приемочными сценариями.

## [0.0.1] — 2026-11-10

### Добавлено

- Каркас репозитория: README, AGENTS.md, LICENSE, CI, шаблоны issue (M0, I0).
- ADR-0016 (пивот на учет времени), ADR-0017 (закрытие дня 00:00 MSK),
  ADR-0018 (закрытая подписка), ADR-0019 (дневное начисление долей).
- Видение, глоссарий, карта переноса прежней версии (`migration-map.md`):
  треки A–H и ADR-0001…0015 сохранены со статусами перенос/адаптация/заменено.
- Планы: `documentation-roadmap.md`, `docs-plan.md` (D0–D9),
  `development-plan.md` (M0–M9).
