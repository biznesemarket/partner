# Модель данных

| Поле | Значение |
|---|---|
| Версия | 2.1 |
| Дата | 2026-11-13 |
| Заменяет | D5-data-model (перенос + сессии, heartbeat, дни, версии) |
| Связанные | overview, ADR-0016, ADR-0019, apps/partner_cli/schema.sql (прототип), apps/partner_server/models.py (реализация), migrations/versions/0001_initial.py, migrations/versions/0002_m6.py |
| Статус | Реализовано в M4–M6: ORM + миграции Alembic, SQLite (demo) и PostgreSQL (prod) |

## 1. Реализация M4 и целевая схема

Схема реализована в итерациях M4–M6 (`apps/partner_server/models.py`,
16 таблиц) как ORM SQLAlchemy 2.0 с metadata-driven миграциями Alembic
(`0001_initial` — база, `0002_m6` — споры и уведомления, идемпотентная
через checkfirst). Один и тот же код моделей работает
на PostgreSQL (prod) и SQLite (режим embedded/demo — развитие прототипа
CLI). Прототипная схема `apps/partner_cli/schema.sql` сохранена для CLI-режима
и по семантике совпадает с серверной.

Отличия серверной модели от прототипной (добавлены поля, потребные серверу):

- `policies.params_json` — переменные политики (policy.schema.json) в JSON;
- те же единицы хранения: время — секунды/ISO UTC в TEXT, деньги — копейки
  INTEGER, доля — базисные пункты TEXT (Decimal). Никаких float;
- округления всегда в коде (`partner_cli.calc`), не в БД.

Различие диалектов ограничено типами (TIMESTAMPTZ в PG при вынесении
дат в отдельные поля; в M4 время — ISO-строки UTC в обоих диалектах —
это осознанное упрощение миграции, отмечено для ревизии в M8).

## 2. Сущности

| Таблица | Назначение | Ключи |
|---|---|---|
| tenants | организации-клиенты | tenant_id |
| projects | проекты организации | project_id, FK tenant |
| participants | люди в организации | participant_id (человек), status |
| identities | аккаунты платформ ↔ человек (ADR-0009) | identity_id, FK participant, forge, forge_login |
| policies | версии политик проекта | policy_id, FK project, policy_version, sha256 |
| rate_versions | история ставки | FK policy, effective_from, value_cents |
| valuation_versions | история оценки | FK policy, effective_from, value_rub, adr_ref |
| sessions | сессии работы с агентом | session_id, FK participant/project, task_ref, branch |
| heartbeats | сырые события активности | event_id (UUID), FK session, seq, received_at |
| intervals | классифицированные интервалы | interval_id, FK session, class, start/end UTC |
| day_records | закрытые дни | day_id, participant, date (пояс), paid_seconds, amount_cents, share_bp |
| adjustments | компенсирующие записи | adjustment_id, FK day_record, reason, approved_by |
| snapshots | месячные снапшоты | snapshot_id, month, sha256, git_ref |
| flags | флаги дня (cap_applied, autonomous_cap_applied, no_task_link…) | FK day_record, code, detail |
| audit_logs | аудит действий и инцидентов (M5): кто, когда, старое/новое | audit_id, ts, actor, entity, action |
| disputes | споры дня (M6): разработчик помечает, директор решает в окне 3 дня | dispute_id, FK day_record, status open/resolved/rejected/expired, window_deadline, interval_refs |
| notifications | очередь доставки дайджестов/событий (M6): канал, статус, attempts | notification_id, kind, recipient, channel, status pending/sent/failed, payload_json, text |

Единицы хранения: время — секунды (UTC), деньги — копейки (BIGINT),
доля — базисные пункты (share_bp = доля × 10000). Никаких float.

## 2.1 HTTP API (M4–M6)

| Эндпоинт | Метод | Назначение |
|---|---|---|
| /healthz | GET | healthcheck + проверка БД |
| /v1/sessions | POST | создание сессии (идемпотентно: повтор → duplicate) |
| /v1/heartbeats | POST | прием пульса + интервала; идемпотентность по event_id; повтор seq → 422 duplicate_seq |
| /v1/days/{date} | GET | чтение дня: закрытый — из day_records (+ adjustments, споры, эффективная сумма), незакрытый — preview-расчет |
| /v1/days/{date}/close | POST | закрытие дня (ADR-0017); идемпотентно — повтор дает тот же sha256; после закрытия — дайджест в очередь уведомлений |
| /v1/days/{date}/explain | GET | объяснение начисления: интервалы, union-секунды, флаги, версии |
| /v1/status/{participant_id} | GET | месячный итог участника |
| /v1/status/summary | GET | сводка директора за текущий месяц |
| /v1/digest/day/{date} | GET | дневной дайджест (M6.1): числа только из БД + детерминированный текст; расхождение → day_mismatch |
| /v1/digest/month/{month} | GET | месячный дайджест (M6.2): часы, вклад ₽, доли, остаток пула, pending |
| /v1/days/{date}/disputes | POST | спор дня (M6.4): окно 3 дня; один открытый спор на день |
| /v1/disputes/{id}/resolve | POST | решение директора; approved → adjustment (append-only) |
| /v1/disputes | GET | список споров, фильтр по статусу |
| /v1/notifications | GET | журнал доставки дайджестов: канал, статус, attempts/last_error |
| /v1/policies | POST | новая версия политики (M5, иммутабельная) |
| /v1/rate-versions | POST | новая версия ставки (M5, заднее число → 422 V-04) |
| /v1/audit | GET | журнал действий и инцидентов (M5) |
| /v1/legal, /v1/legal/gates/{id}, /v1/legal/enable, /v1/legal/export | GET/POST | юридический модуль M7: гейты G-01…G-08, включение, пакет юристу |
| /v1/snapshots/{month}, /v1/snapshots | POST/GET | месячные снапшоты долей M7 (sha256 + git-якорь) |
| /v1/metrics | GET | эксплуатационные метрики M8 (Prometheus text, только агрегаты) |
| /v1/license | GET | состояние лицензии подписки M9.1 (ADR-0018) |
| /v1/devices, /v1/devices/{id}/revoke | POST/GET | устройства источников M9: регистрация (секрет выдается один раз), отзыв; подпись heartbeat X-Partner-Sign (threat-model §4) |

Аутентификация: Bearer при заданном PARTNER_API_TOKEN; demo-режим без токена
— только local. Изоляция tenants (403 между организациями) — M5.
Подпись устройства — необязательные заголовки X-Partner-Device/X-Partner-Sign;
PARTNER_REQUIRE_DEVICE_SIGN=1 делает их обязательными (отклонение 422).
Лицензионный гейт M9.1: license invalid/expired — закрытие дня 402.

Договор объединения (union) зафиксирован на сервисном слое: сервер не
суммирует перекрывающиеся интервалы — пересечение собирается в
`partner_cli.calc.union_intervals` при сборке дня (R3, T-04). Правила на
стыке БД и калькулятора: сессия без task_ref → класс idle + флаг
no_task_link; сессия без агента → track_only (T-10).

## 3. ERD (текстовый)

```text
tenants 1──n projects 1──n policies 1──n rate_versions
                            │              └── n valuation_versions
                            ├── n sessions 1──n heartbeats
                            │      └── 1──n intervals
participants 1──n identities ──┘
participants 1──n day_records 1──n adjustments ←── disputes
                     ├── n flags
                     ├── n notifications (дайджесты и события, M6)
                     └── n snapshots (агрегация месяца)
```

## 4. Ключевые ограничения целостности

| Правило | Реализация |
|---|---|
| Дневная запись после закрытия не обновляется | UPDATE запрещен триггером/проверкой статуса; правка только через adjustments |
| Уникальность event_id | PK heartbeats = event_id (идемпотентность, защита от replay) |
| seq монотонный в сессии | составной уникальный индекс (session_id, seq) |
| rate/valuation версии не задним числом | проверка в коде + V-04 в policy_lint |
| Сессия без task_ref | флаг no_task_link, интервалы не оплачиваются |
| День в поясе проекта | date хранится вместе с tz; разрез по 00:00 пояса |
| Один открытый спор на день | проверка в disputes.open_dispute → dispute_already_open |
| Решение по спору — в окне 3 дней | window_deadline; просрочка → expired + dispute_window_expired |
| Корректировка не меняет день | adjustment (append-only): эффективная сумма = amount_cents + Σ delta_cents |

## 5. Мультиинстанс и идентичность (наследие ADR-0009/0014)

- Человек (`participants`) ≠ аккаунт (`identities`): у одного человека
  может быть один аккаунт на платформу (дефолт), исключение — по
  обоснованию, которое разрешает директор.
- Агрегация параллельных сессий (union) — на уровне человека внутри
  организации (wip_scope: person), двойной оплаты не бывает.
- Исторические записи не переписываются при смене привязки аккаунтов;
  новая привязка действует с даты.

## 6. Миграция SQLite → PostgreSQL (план I4)

| Шаг | Действие |
|---|---|
| 1 | Экспорт прототипных таблиц в CSV/JSONL с SHA-256 манифестом |
| 2 | Импорт в PG-схему core (проекция) идемпотентно по манифесту |
| 3 | Паритет-проверки: суммы копеек, counts, sha256 совпадают |
| 4 | Переключение записи на PG; SQLite остается read-only архивом |
| 5 | git-якоря не переносятся — они уже в git |

## 7. Приватность

Хранятся только метаданные активности: события ввода (счетчики), фокус,
вызовы инструментов, коммиты. Не хранятся: содержимое файлов, переписка,
скриншоты, код (кроме ссылок на ветки/задачи). Сырые события удаляются
через raw_events_retention_days (90); интервалы и дневные записи —
бессрочно как доказательная база.

## 8. Примеры строк

```text
day_records: 2026-11-12, participant=p-01, paid_seconds=23760 (6,6 ч),
             amount_cents=330000, share_bp=330, flags=[],
             rate_version=1, valuation_version=1, policy_version=1
intervals:   10:00–12:00 active_human, session=s-9, signals=412
heartbeats:  4f8c…, seq=1187, received_at=2026-11-12T07:00:30Z, signed
```

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 2.1 | 2026-11-13 | M6: disputes и notifications, миграция 0002_m6, API дайджестов и споров, эффективная сумма дня |
| 2.0 | 2026-11-13 | M4: реализация ORM + Alembic, HTTP API, договор union на сервисном слое |
| 1.0 | 2026-11-13 | Сессии, heartbeat, интервалы, дневные записи, версии политики |
