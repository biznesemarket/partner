# API «Партнёра» — OpenAPI-выжимка

| Поле | Значение |
|---|---|
| Версия | 1.3.0 |
| Назначение | Справочник клиента по HTTP API; полная спецификация — `GET /openapi.json`, интерактивно — `GET /docs` |
| Источник | `python tools/openapi_digest.py` (генерируется из `app.openapi()`) |
| Статус | approved — синхронность с кодом проверяет `tests/test_openapi_digest.py` |

## 1. Аутентификация

- `Authorization: Bearer <PARTNER_API_TOKEN>` — глобальный административный токен;
- tenant-токены из `PARTNER_TENANT_TOKENS` — роли `director`, `participant`, `lawyer`;
- вебхуки коннекторов аутентифицируются HMAC-подписью платформы, а не Bearer;
- без токена открыты `/healthz`, `/v1/healthz`, `/v1/metrics`, `/v1/license`.

`/dashboard` — статическая страница; данные она запрашивает у `/v1`
с токеном из `sessionStorage`.

## 2. Эндпоинты

### Система

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/healthz` | Проверка живости API и БД для healthcheck. |
| GET | `/v1/license` | M9.1: состояние лицензии подписки (ADR-0018) — без PII, открыто для мониторинга; используется дежурным и дашбордом установки. |
| GET | `/v1/metrics` | M8.4: эксплуатационные метрики в формате Prometheus text. |

### Сессии и события

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/v1/heartbeats` | Принять heartbeat: идемпотентно по event_id, с опциональной подписью устройства. |
| POST | `/v1/sessions` | Создать сессию участника; повтор с тем же session_id идемпотентен. |

### Устройства

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/devices` | Список устройств участника — без секретов (они не хранятся в открытом виде для витрины). |
| POST | `/v1/devices` | Регистрация устройства агента: секрет выдается ОДИН раз (не читается повторно). |
| POST | `/v1/devices/{device_id}/revoke` | Отозвать устройство участника; последующие подписанные heartbeat отклоняются. |

### Дни

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/days/{date}` | День участника: часы, сумма, доля и флаги; до закрытия — preview. |
| POST | `/v1/days/{date}/close` | Закрыть день: неизменяемая запись, идемпотентно, поздние события — в adjustments. |
| GET | `/v1/days/{date}/explain` | Объяснение начисления: интервалы, union-секунды по классам, капы, версии. |

### Статусы и флаги

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/flags` | Флаги закрытых дней для дашборда директора (tenant/project-scoped). |
| GET | `/v1/status/summary` | Сводка директора: суммы по участникам за месяц (по умолчанию текущий). |
| GET | `/v1/status/{participant_id}` | Месячный статус участника: закрытые дни, суммы, доля и флаги. |

### Дайджесты и уведомления

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/digest/day/{date}` | Дайджест закрытого дня: числа воспроизводимы из БД (DoD M6). |
| GET | `/v1/digest/month/{month}` | Месячный дайджест: часы, вклад, доли, остаток пула, pending. |
| GET | `/v1/notifications` | Журнал доставки дайджестов: канал, статус, attempts/last_error (M8.4). |

### Споры

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/v1/days/{date}/disputes` | Спор дня: разработчик помечает день (и опционально интервалы). |
| GET | `/v1/disputes` | Список споров (директор): фильтр по статусу, проекту и месяцу дня. |
| POST | `/v1/disputes/{dispute_id}/resolve` | Решение директора в окне 3 дней; approved → adjustment (append-only). |

### Политика

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/v1/policies` | Новая версия политики: иммутабельна, действует с даты (R-4, threat-model §3). |
| POST | `/v1/rate-versions` | Новая версия ставки. Заднее число запрещено (V-04, R-2). |

### Аудит

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/audit` | Журнал действий и инцидентов (директор). Append-only. |

### Юридический модуль и снапшоты

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/legal` | Статус юридического модуля и гейты G-01…G-08 (legal-module.md §4). |
| POST | `/v1/legal/enable` | Включение legal.enabled: true — только при снятых G-01…G-08 (M7.3). |
| POST | `/v1/legal/export` | Пакет юристу (M7.2): политика, снапшоты, гейты, вопросы, шаблоны M7.4. |
| POST | `/v1/legal/gates/{gate_id}` | Юрист снимает (done) или возвращает (open) гейт; done требует evidence. |
| GET | `/v1/snapshots` | Список месячных снапшотов (директор/юрист) в рамках tenant/project. |
| POST | `/v1/snapshots/{month}` | Месячный снапшот долей (M7.1): вклад ₽ и доли участников, sha256 + git-якорь. |
| GET | `/v1/snapshots/{snapshot_id}` | Снапшот с payload и подписываемым листом (render). |

### Коннекторы

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/connectors/events` | Журнал принятых доставок (без сырых payload) — диагностика коннектора. |
| GET | `/v1/connectors/facts` | Факты репозитория проекта: ветки, коммиты, PR, issues, приемка. |
| POST | `/v1/connectors/{forge}/import` | Импорт фактов из API платформы (backfill) — идемпотентно по delivery_id. |
| POST | `/v1/connectors/{forge}/webhook` | Прием вебхука GitHub/Forgejo/Gitea с проверкой HMAC-подписи. |

### Прочее

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | Служебный ответ: имя сервиса, версия и ссылка на интерактивный OpenAPI. |

## 3. Схемы запросов

| Схема | Обязательные поля | Все поля |
|---|---|---|
| `ActorIn` | `actor` | `actor` |
| `DeviceIn` | `participant_id` | `participant_id`, `label` |
| `DisputeIn` | `participant_id`, `opened_by`, `reason` | `participant_id`, `opened_by`, `reason`, `interval_ids` |
| `DisputeResolveIn` | `decided_by`, `decision` | `decided_by`, `decision`, `delta_cents`, `resolution` |
| `GateIn` | `status`, `actor` | `status`, `evidence`, `actor` |
| `HeartbeatIn` | `event_id`, `session_id`, `seq`, `cls`, `start_utc`, `end_utc` | `event_id`, `session_id`, `seq`, `cls`, `start_utc`, `end_utc`, `signals`, `received_at_utc` |
| `ImportIn` | `project_id`, `repo` | `project_id`, `repo`, `forge`, `source`, `delivery_id`, `items` |
| `PolicyIn` | `project_id`, `params_json`, `approved_by` | `project_id`, `params_json`, `approved_by`, `reason` |
| `RateVersionIn` | `policy_id`, `effective_from`, `value_cents`, `reason`, `approved_by` | `policy_id`, `effective_from`, `value_cents`, `reason`, `approved_by` |
| `SessionIn` | `session_id`, `participant_id`, `project_id`, `started_at_utc` | `session_id`, `participant_id`, `project_id`, `task_ref`, `branch`, `agent_attached`, `started_at_utc` |

## 4. Ошибки

Ошибки возвращаются как `{ "detail": { "code": "...", "detail": "..." } }`.
Основные коды: `no_session`, `no_policy`, `day_other_project`,
`cross_tenant_denied`, `participant_scope_denied`, `day_project_conflict`,
`legal_state_busy`, `manual_only_policy_not_supported`, `version_backdated`,
`device_signature_required`, `license_invalid`, `license_expired`.

Коды и HTTP-статусы совпадают с `docs/03-architecture/overview.md` §6
и `docs/07-ops/installation.md`.
