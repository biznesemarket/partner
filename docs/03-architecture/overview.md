# Архитектура «Партнера» (C4 L1–L3)

| Поле | Значение |
|---|---|
| Версия | 2.0 |
| Дата | 2026-11-14 |
| Назначение | Полное описание архитектуры v1.2 по модели C4: контекст, контейнеры, компоненты, ключевые потоки, API |
| Аудитория | разработчик клиента и продукта |
| Статус | approved — C4 L1–L3 сверен с кодом v1.2 |
| Связанные | data-model.md, connectors.md, agent-kit.md, time-accounting.md, ADR-0016, ADR-0017, ADR-0021, installation.md |
| Заменяет | overview.md 1.0 (сокращенный обзор) |

## 1. Назначение и принципы

1. **Гибридное хранилище (ADR-0016).** Поток событий времени → БД
   (истина для событий); принятые дневные итоги и месячные снапшоты →
   якорь в git (доказательная база для споров и юриста).
2. **Self-hosted, мультитенантность.** Tenant → проекты → участники;
   данные клиента не покидают его периметр. Изоляция tenant проверяется
   на каждом запросе и подтверждена тестами `tests/test_tenant.py`.
3. **Правила в репозитории клиента.** `policy.yaml` + `AGENTS.md` —
   источник правил; сервер читает и версионирует политику.
4. **Deny by default.** Bearer-токен на всех данных, tenant-токены
   `PARTNER_TENANT_TOKENS`, HMAC устройства и HMAC вебхуков; открыты
   только `/healthz`, `/v1/healthz`, `/v1/metrics`, `/v1/license`.
   Роли tenant-токенов (T-06): `director` — агрегаты и управление,
   `participant` — только свои данные, `lawyer` — только legal/snapshots;
   отказы и выход за `participant_id` пишутся в аудит.
5. **Append-only.** Интервалы, закрытые `day_records`, корректировки
   `adjustments` и снапшоты не перезаписываются; правки — только
   компенсирующими записями (ADR-0021).
6. **Минимум сервисов в MVP.** Витрина — дайджест + CLI (ADR-0018);
   дашборд не строим до спроса.

## 2. C4 L1 — контекст системы

```text
[Директор] ────────(1) политика, решения, сводки──────────────┐
[Разработчик] ─────(2) сессии и heartbeat через agent-kit─────┤
[ИИ-агент] ────────(3) MCP: старт, heartbeat, статус──────────┤
[Git-платформа] ───(4) вебхуки push / PR / issues─────────────┤
                                                               ▼
                                                         [ Партнер ]
                                                               │
        ┌──────────────────(5) дайджесты────────────────────────┤
        ▼                                                      │
[Почта / webhook / agent_chat]                                 │
                                                               │
[Юрист] ◀──────────(6) снапшоты и пакет документов────────────┤
                                                               │
[Git-репозиторий проекта] ◀──(7) git-якорь месячного снапшота──┘
```

| # | Откуда → куда | Что передается | Канал и защита |
|---|---|---|---|
| 1 | Директор → сервер | политика, версии ставки, решения по спорам, запросы сводок | HTTPS, Bearer |
| 2 | Разработчик → agent-kit → сервер | сессии, интервалы heartbeat, сигналы активности | HTTPS, Bearer + HMAC устройства |
| 3 | ИИ-агент → MCP-сервер agent-kit | вызовы инструментов `partner_*`, подтверждение человека | stdio JSON-RPC |
| 4 | Git-платформа → сервер | push, pull_request, issues: только метаданные | HTTPS, HMAC вебхука |
| 5 | Сервер → каналы | дневной и месячный дайджест, уведомления о спорах | SMTP / webhook / `agent_chat` push + журнал |
| 6 | Сервер → юрист | политика, снапшоты, гейты, вопросы, шаблоны | CLI-экспорт, ручная передача |
| 7 | Сервер → репозиторий | sha256 и git-якорь месячного снапшота | git CLI из процесса `legal.py` |

Границы доверия: коннектор читает только метаданные платформы; код,
промпты и содержимое файлов не покидают репозиторий клиента. Сервер не
вызывает внешние модели.

## 3. C4 L2 — контейнеры

```text
[VS Code / CLI / MCP-клиент]
        │ stdio или CLI
        ▼
[partner-agent-kit] ──HTTPS /v1/sessions, /v1/heartbeats──▶ [partner-server]
                                                                   │ SQL
[partner-cli] ─────────HTTPS /v1/...──────────────────────────────▶│
                                                                   ▼
[Git-платформа] ──вебхуки────────────────────────────────▶ [PostgreSQL 16]
[partner-connector] ──API платформы──▶ [Git-платформа]             ▲
[partner-connector] ──/v1/connectors/{forge}/import───────────────┘
[partner-server] ──SMTP / webhook──▶ [каналы уведомлений]
[repo-pack] ──policy.yaml, AGENTS.md, workflow──▶ [репозиторий клиента]
```

| Контейнер | Технология | Ответственность | Данные и связи |
|---|---|---|---|
| `partner-agent-kit` | Python CLI/daemon, MCP stdio, VS Code-обертка | сессии, heartbeat 30 с, классификация, офлайн-буфер, HMAC устройства | `~/.config/partner/agent.yaml`, `agent-buffer.jsonl`; HTTPS к серверу |
| `partner-server` | Python, FastAPI, планировщик в процессе | API, прием событий, расчет и закрытие дня, дайджесты, споры, юрмодуль, вебхуки | PostgreSQL (prod), SQLite (demo), `legal.yaml`; SMTP/webhook |
| PostgreSQL 16 | PostgreSQL | система записи: `tenants`, `projects`, `participants`, `sessions`, `heartbeats`, `intervals`, `day_records`, `adjustments`, `flags`, `disputes`, `snapshots`, `connector_events`, `repo_facts`, `audit_log` | только внутренняя сеть compose |
| `partner-cli` | Python, Typer | мастер `init`, локальные `day`/`explain`/`snapshot`, серверные `digest`, `dispute*`, `policy set`, `snapshot-close`, `legal-*`, `export-legal` | SQLite прототипа для demo; HTTPS к серверу |
| `partner-connector` | Python CLI | backfill веток, PR и issues из API платформы и импорт в сервер | без своей БД; API платформы + HTTPS к серверу |
| `repo-pack` | Markdown/YAML/workflow | устанавливаемые в репозиторий клиента правила, политика и CI-проверка | файлы в git клиента |
| Каналы уведомлений | SMTP, HTTP webhook, `agent_chat` (push в адаптер + журнал) | доставка дайджестов и уведомлений | внешние адресаты; `PARTNER_AGENT_CHAT_URL/TOKEN` (B-03) |

## 4. C4 L3 — компоненты сервера

```text
HTTP API: routes.py, connector_routes.py
   ├─ auth / tenant scope ──────────────────────────────┐
   ├─ sessions + heartbeat ─▶ service.ingest_heartbeat ─┼─▶ heartbeats, intervals
   ├─ day close ───────────▶ dayclose / service.close_day
   │                              └─▶ service.compute_day
   │                                    ├─ service.day_intervals
   │                                    ├─ antifraud.apply
   │                                    └─ partner_cli.calc.calc_day (golden)
   ├─ digests ─────────────▶ digest.py ──▶ day_records, adjustments, flags, repo_facts
   ├─ disputes ────────────▶ disputes.py ─▶ adjustments
   ├─ connectors ──────────▶ connectors.py ─▶ connector_events, repo_facts
   └─ legal ───────────────▶ legal.py ──────▶ snapshots, gates, export
```

| Компонент | Модуль | Ответственность и инварианты |
|---|---|---|
| API и доступ | `routes.py`, `connector_routes.py` | Bearer-токен; `PARTNER_TENANT_TOKENS` привязывает токен к tenant; `X-Partner-Tenant` сужает область admin/demo; проект — из `project_id` или первый проект tenant. Ошибки — JSON `{code, detail}` |
| Прием heartbeat | `service.ingest_heartbeat` | `event_id` — ключ идемпотентности; повтор → `duplicate` без записи; `(session_id, seq)` уникален; класс из белого списка; `end_utc > start_utc`; интервал append-only |
| Сборка дня | `service.day_intervals`, `local_pieces` | окно дня по поясу проекта; разрез по локальной полуночи (T-05); сессия без `task_ref` → `idle` + `no_task_link`; сессия без агента → `manual_track_only`; heartbeat присоединяется по `interval_id` |
| Антифрод | `antifraud.py` | флаги `night_autonomous_spike`, `synthetic_input`, `idle_no_signal`, `clock_skew`, `late_rejected`; флаги не блокируют выплату; `late_rejected` исключает событие из расчета, `idle` не оплачивается |
| Policy engine и golden-калькулятор | `service.load_policy`, `partner_cli.calc` | версии политики, ставки и оценки; union пересечений (T-04); разрез по полуночи; капы дня и автономного времени; коэффициенты классов; числа совпадают с T-01…T-12 |
| Закрытие дня | `dayclose.py`, `service.close_day` | планировщик в 00:00 пояса проекта + догоняющий прогон при старте; запись `day_record` неизменяема; повтор с тем же расчетом → `duplicate`; изменившийся расчет → `adjustment` (ADR-0021); лицензия `invalid/expired` → 402, чтение данных остается доступным |
| Дайджесты и уведомления | `digest.py`, `notify.py` | только закрытые дни; числа из `day_records` + `adjustments`; текст детерминирован; месячный остаток пула — по проекту; каналы с повтором (`PARTNER_NOTIFY_ATTEMPTS`) и статусами `pending/sent/failed` |
| Споры | `disputes.py` | спор только по закрытому дню; один открытый спор на день; окно `PARTNER_DISPUTE_DAYS` (3 суток); `approved` → `adjustment`, `rejected`/`expired` — без изменения суммы; всё в аудите |
| Коннекторы | `connectors.py`, `connector_routes.py`, `partner_connectors` | HMAC вебхука, дедупликация доставки, факты только из метаданных; заполнение пустых `task_ref`/`branch` сессий; `no_output` по фактам и `tool_calls`; backfill через API |
| Юридический модуль | `legal.py` | по умолчанию выключен; гейты G-01…G-08; снапшот месяца иммутабелен: sha256 + git-якорь; пакет юристу без персональных данных. Документ `legal-module.md` ожидает ревью юриста (D7) |
| Аудит, метрики, лицензия | `audit.py`, `ops.py`, `licensing.py` | append-only журнал действий и инцидентов; `/v1/metrics`; состояния лицензии `demo/active/grace/invalid/expired` |
| Витрина директора | `dashboard.html` | `/dashboard` — самодостаточная HTML-страница без данных; сводка/флаги/споры/снапшоты/экспорт через `/v1` с Bearer-токеном; токен только в `sessionStorage` |

## 5. Ключевые потоки

### 5.1 Прием heartbeat

1. `partner-agent start` вызывает `POST /v1/sessions`; повтор `session_id`
   идемпотентен.
2. Каждые 30 секунд агент отправляет `POST /v1/heartbeats` с `event_id`,
   монотонным `seq`, классом, интервалом `[start_utc, end_utc)` и сигналами.
3. Сервер проверяет токен, tenant, при необходимости HMAC устройства, и
   отклоняет replay (`event_id`) и повтор `seq`.
4. Heartbeat и интервал пишутся в БД; на этом шаге деньги не считаются.
5. При офлайне события лежат в буфере и отправляются в порядке `seq`;
   события старше `grace_hours` получают `late_rejected`.

### 5.2 Закрытие дня

1. Планировщик в 00:00 пояса проекта (или `POST /v1/days/{date}/close`)
   выбирает незакрытые дни с интервалами; при старте выполняется
   догоняющий прогон `close_catch_up`.
2. `compute_day` загружает активную политику и ставку на дату, собирает
   фрагменты дня, применяет антифрод и golden-калькулятор.
3. Если записи нет — создается `day_record` (`closed=1`, `closed_at`) и
   флаги дня.
4. Если запись есть и расчет совпал — ответ `duplicate`; если расчет
   изменился — создается компенсирующий `adjustment` на дельту, сама
   запись не меняется (ADR-0021).
5. После закрытия доступны дайджест и уведомления; повторный запуск
   идемпотентен.

### 5.3 Поздние события и корректировки

1. Событие в пределах `grace_hours` (по умолчанию 24 ч) принимается и
   учитывается при следующем закрытии/пересчете дня.
2. Событие за грейсом отклоняется (`late_rejected`) и не оплачивается.
3. Закрытая сумма не перезаписывается: эффективная сумма дня =
   `day_record.amount_cents + Σ adjustments.delta_cents`.
4. Ручная корректировка проходит только через спор дня и решение
   директора; каждая корректировка append-only и попадает в аудит.

### 5.4 Спор дня

1. Разработчик открывает спор по закрытому дню с причиной и, опционально,
   списком `interval_id`.
2. Сервер проверяет принадлежность интервалов участнику и проекту;
   второй открытый спор по тому же дню запрещен.
3. Дедлайн — `opened_at + PARTNER_DISPUTE_DAYS` (3 суток); после дедлайна
   спор истекает и решение невозможно.
4. Директор принимает `approved` с дельтой или `rejected`; при `approved`
   создается `adjustment`, закрытый `day_record` не меняется.
5. Дайджест и `GET /v1/days/{date}` показывают эффективную сумму; событие
   фиксируется в аудите и очереди уведомлений.

### 5.5 Коннекторы

1. Платформа вызывает `POST /v1/connectors/{forge}/webhook`; сервер
   проверяет HMAC по сырому телу, находит проект по `owner/name`.
2. `delivery_id` дедуплицирует повторные доставки; неподдерживаемые
   события журналируются без фактов.
3. Из события извлекаются только метаданные: ветки, коммиты, PR, issues,
   метки, счетчики.
4. Коннектор заполняет пустые `task_ref`/`branch` сессий по соглашению
   `dev/<логин>/<тема>`; факты используются флагом `no_output`.
5. Backfill `partner-connector sync-repo` собирает ветки/PR/issues через
   API платформы и отправляет идемпотентный пакет импорта.

### 5.6 Дайджест и доставка

1. Дневной дайджест строится только по закрытому дню; часы по классам
   пересобираются из интервалов БД через golden-калькулятор.
2. Месячный дайджест суммирует закрытые дни проекта, корректировки,
   остаток пула и pending-пункты (открытые споры, незакрытые дни).
3. Текст рендерится детерминированно и попадает в уведомление и CLI.
4. Недоставленные уведомления повторяются при закрытии дня и старте
   сервера; статусы видны в `GET /v1/notifications`.

### 5.7 Дашборд директора

1. `/dashboard` отдает статическую страницу без встроенных данных; токен
   вводится в браузере и хранится только в `sessionStorage`.
2. Сводка — `GET /v1/status/summary`: исходные суммы и эффективные с
   учетом append-only `adjustments`.
3. Флаги — `GET /v1/flags`: антифрод/кап-флаги месяца, tenant/project-scoped.
4. Споры и снапшоты — существующие `/v1/disputes` и `/v1/snapshots`;
   решение спора и закрепление месяца идут через те же API.
5. Экспорт CSV/JSON формируется в браузере из уже загруженных ответов;
   сервер не хранит файлы экспорта.

## 6. API v1.3

Краткая выжимка для клиента — [`api-reference.md`](api-reference.md)
(генерируется из `app.openapi()`); полная спецификация — `/docs`
(OpenAPI FastAPI). Группы:

| Группа | Эндпоинты |
|---|---|
| Система | `GET /healthz`, `GET /v1/healthz`, `GET /v1/metrics`, `GET /v1/license` |
| Сессии и события | `POST /v1/sessions`, `POST /v1/heartbeats` |
| Устройства | `POST /v1/devices`, `POST /v1/devices/{device_id}/revoke`, `GET /v1/devices` |
| Дни | `GET /v1/days/{date}`, `POST /v1/days/{date}/close`, `GET /v1/days/{date}/explain` (параметр `participant_id` во всех) |
| Статусы | `GET /v1/status/summary?month=YYYY-MM`, `GET /v1/status/{participant_id}`, `GET /v1/flags?month=YYYY-MM` (флаги для дашборда) |
| Дайджесты | `GET /v1/digest/day/{date}`, `GET /v1/digest/month/{month}` (параметр `participant_id`) |
| Споры | `POST /v1/days/{date}/disputes`, `POST /v1/disputes/{dispute_id}/resolve`, `GET /v1/disputes` (status, month) |
| Уведомления | `GET /v1/notifications` |
| Политика | `POST /v1/policies`, `POST /v1/rate-versions` |
| Аудит | `GET /v1/audit` |
| Юрмодуль | `GET /v1/legal`, `POST /v1/legal/gates/{gate_id}`, `POST /v1/legal/enable`, `POST /v1/legal/export` |
| Снапшоты | `POST /v1/snapshots/{month}`, `GET /v1/snapshots`, `GET /v1/snapshots/{snapshot_id}` |
| Коннекторы | `POST /v1/connectors/{forge}/webhook`, `POST /v1/connectors/{forge}/import`, `GET /v1/connectors/facts`, `GET /v1/connectors/events` |

Все данные-эндпоинты требуют Bearer, кроме перечисленных открытых;
вебхук коннектора аутентифицируется HMAC-подписью платформы, а не Bearer.
Статическая страница `/dashboard` не содержит данных: она запрашивает их
у `/v1` с токеном из `sessionStorage`.
`project_id` в query сужает запрос до проекта tenant; без него берется
первый проект tenant. Ошибки возвращаются как `{ "detail": { "code": "...",
"detail": "..." } }`; коды домена (`day_not_closed`, `cross_tenant_denied`,
`version_backdated` и т. п.) совпадают с текстом документа.

## 7. Развертывание

`deploy/docker-compose.yml` поднимает:

| Сервис | Роль |
|---|---|
| `db` | PostgreSQL 16, внутренняя сеть, healthcheck |
| `migrate` | разово `alembic upgrade head` перед стартом сервера |
| `server` | FastAPI + планировщик (`PARTNER_SCHEDULER=1`), наружу только `server:8080` |
| `dayclose` | ручной догоняющий прогон, профиль `tools` |
| `backup` | ежедневный дамп БД по `PARTNER_BACKUP_AT` |

Планировщик живет в процессе сервера: поставка рассчитана на один
экземпляр `server`. Горизонтальное масштабирование потребует вынести
закрытие дня и рассылку в отдельный воркер с блокировкой.

## 8. Эволюция и риски

| Риск | Мера |
|---|---|
| Накрутка времени | классы, капы, `no_output`, антифрод-флаги, споры |
| Потеря БД | бэкапы compose + git-якоря итогов (ADR-0020) |
| Смешение tenant/проектов | `PARTNER_TENANT_TOKENS`, фильтры и тесты изоляции |
| Повторное закрытие дня | ADR-0021: неизменяемая запись + append-only компенсации |
| Смена git-платформы | коннекторный контракт и `repo_facts`, метки как машиночитаемый контракт |
| Спор о расчете | `explain`, sha256 дня, снапшоты и аудит |
| Единственный экземпляр планировщика | вынос в отдельный воркер до горизонтального масштабирования |
| Юридические формулировки | `legal-module.md` проходит ревью юриста (D7), код работает при `legal.enabled: false` |

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 1.0 | 2026-11-13 | Новая архитектура: сервер + БД + agent-kit + repo-pack |
| 1.1 | 2026-11-14 | Коннекторы GitHub/Forgejo/Gitea реализованы (вебхуки, факты) |
| 2.0 | 2026-11-14 | Полный C4 L1–L3 по коду v1.2: контейнеры, компоненты, потоки, фактический API, deployment |
| 3.0 | 2026-11-14 | v1.3: дашборд `/dashboard`, `/v1/flags`, RBAC-роли, agent_chat push, legal lock |
