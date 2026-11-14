# Backlog дальнейшей разработки «Партнера»

| Поле | Значение |
|---|---|
| Версия | 1.1 |
| Дата | 2026-11-14 |
| Назначение | Честный разрыв между заявленным v1.0.0 и фактическим кодом; очередь работ |
| Статус | draft (внутренний план, не публикуется на docs-сайте) |
| Связанные | development-plan.md, docs-plan.md, overview.md, connectors.md, threat-model.md |

## 1. Что уже работает

- Сервер FastAPI: сессии, идемпотентные heartbeat, union интервалов,
  закрытие дня, explain, статусы, антифрод-флаги, аудит, споры,
  дайджесты, снапшоты, юридический модуль, лицензирование, метрики.
- CLI: `init`, `status`, `day`, `explain`, `snapshot`, `policy set`,
  `dispute`, `digest`, `legal-*`, `export-legal`.
- v1.1: `partner-agent` (сессии, heartbeat 30 с, офлайн-буфер, подпись),
  MCP stdio, VS Code-обертка; `partner-connector` и вебхуки GitHub/Forgejo
  с фактами `repo_facts`; флаг `no_output`.
- Калькулятор дня с golden-сценариями T-01…T-12; 171 тест; CI Linux +
  Windows + PostgreSQL smoke + package-smoke.
- Поставка Docker Compose, бэкапы, runbook, docs-сайт, акт приемки.

## 2. Разрывы, блокирующие сквозной продукт

| # | Разрыв | Влияние | Приоритет |
|---|---|---|---|
| B-01 | ~~Нет `partner-agent-kit` (IDE-плагин + MCP-сервер): сессии и heartbeat умеет отправлять только HTTP-клиент~~ | закрыто в v1.1: CLI/daemon, офлайн-буфер, подпись, MCP, VS Code | P0 |
| B-02 | ~~Нет `partner-connector-*` (GitHub/Forgejo/Gitea): ветки, PR, задачи, вебхуки~~ | закрыто в v1.1: вебхуки push/PR/issues, `repo_facts`, привязка сессий, backfill | P0 |
| B-03 | Нет транспорта `agent_chat`: канал пишет только в журнал сервера | дайджест не доставляется в чат агента автоматически | P1 |
| B-04 | ~~Флаг `no_output` описан в time-accounting.md и connectors.md, но не реализован~~ | закрыто в v1.1: сигналы tool_calls + факты commit/pull_request; тесты | P1 |
| B-05 | ~~Нет публикации пакета `partner-tracker` (PyPI/приватный индекс)~~ | закрыто v1.2: release-workflow собирает wheel/sdist и `dist-manifest.json`, CI `package-smoke` ставит приватный asset по токену, repo-pack fail-closed (`PARTNER_TRACKER_WHEEL`/`TOKEN`/`SHA256` или `INDEX_URL`) | P1 |
| B-06 | ~~Нет живого прогона PostgreSQL/Docker Compose в CI~~ | закрыто v1.2: job `postgres-smoke` (миграции, seed, uvicorn, /healthz, закрытие дня) и `docker-build` | P1 |

## 3. Технический долг и риски

| # | Риск | Где | Мера |
|---|---|---|---|
| T-01 | ~~`status/summary`, `disputes`, `notifications`, `audit`, `snapshots` не фильтруются по tenant/project~~ | закрыто v1.2: `PARTNER_TENANT_TOKENS`, `DayRecord.project_id`, project-scoped `day_id`, фильтры и тесты изоляции |
| T-02 | ~~`month_digest` считает остаток пула по всем `day_records` без фильтра проекта~~ | закрыто v1.2: фильтр `project_id` в месячном дайджесте/снапшоте, тесты |
| T-03 | ~~Повторное закрытие дня перезаписывает `day_record` при изменившихся данных~~ | закрыто v1.2: ADR-0021, запись неизменяема, компенсации append-only, тесты |
| T-04 | `legal.yaml` пишется без блокировки | `legal.py` | файловая блокировка/атомарная замена, тест конкурентности |
| T-05 | `manual_only_policy: bill` бросает `NotImplementedError` | `calc.py` | оставить явный отказ в API или реализовать по ADR |
| T-06 | Лимиты/пагинация части эндпоинтов и отсутствие RBAC ролей | `routes.py` | роли директор/участник/юрист, аудит доступа |
| T-07 | Демо-ответ мастера содержит ФИО директора tenant #1 | `wizard.py` | обезличить демо-профиль |

## 4. Документация

| # | Работа | Статус |
|---|---|---|
| D-01 | ~~`overview.md` — полный C4 L1–L3 (сервер, БД, agent-kit, коннекторы)~~ | закрыто v1.2 (#8): overview.md 2.0 |
| D-02 | ~~`connectors.md` — протоколы GitHub/Forgejo, подпись вебхуков, ретраи~~ | закрыто v1.2 (#8): connectors.md 1.2 |
| D-03 | ~~`08-guides/morning-developer.md` и `director-day.md`~~ | закрыто v1.2 (#8): оба гайда approved |
| D-04 | `legal-module.md` — ревью юриста (гейты G-01…G-08) | на ревью |
| D-05 | OpenAPI-выжимка для клиента (сейчас — автогенерация `/docs`) | backlog |

## 5. Порядок работ

### v1.1 · Сквозной учет (P0) — сделано 2026-11-14

1. [x] `partner-agent-kit`: протокол сессии, heartbeat 30 с, офлайн-буфер,
   подпись устройства, MCP-инструменты; контракт — в `docs/03-architecture/agent-kit.md`.
2. [x] `partner-connector-github/forgejo`: вебхуки push/pull_request/issues,
   привязка сессии к задаче/ветке, факты коммитов/PR, backfill через API.
3. [x] `no_output`-детект на основе signals и фактов коннектора + тесты.
4. [x] Docker Compose с PostgreSQL в CI (service container) и smoke-тест:
   `postgres-smoke` + `docker-build` в `.github/workflows/partner-ci.yml`
   (issue #5).

### v1.2 · Эксплуатация и доверие (P1)

1. [x] Tenant/RBAC-хардening и тесты изоляции (#4).
2. Транспорт `agent_chat` (адаптер к чату агента) и шаблоны уведомлений.
3. [x] Публикация пакета: release-workflow (wheel/sdist + `dist-manifest.json`),
   CI `package-smoke` (реальный приватный asset по токену), repo-pack
   workflow fail-closed: `PARTNER_TRACKER_WHEEL`/`TOKEN`/`SHA256` или
   `INDEX_URL`, публичный PyPI не используется (issue #6).
4. [x] Документация D4/D6: C4 overview, протоколы коннекторов и два гайда
   (issue #8).
5. [x] Инвариант закрытого дня: ADR-0021, запрет перезаписи, компенсации (#7).

### v1.3 · Витрина (P2)

1. Веб-дашборд директора: сводка, флаги, споры, снапшоты.
2. Отчеты для клиента и экспорт в бухгалтерию.
3. Публичный docs-сайт на домене вендора.

## 6. Правила ведения backlog

- Один пункт = один GitHub issue с меткой (`p0`, `p1`, `p2`, `agent-kit`,
  `connectors`, `docs`, `tech-debt`, `security`).
- Пункт закрывается только с тестом/проверкой и обновлением документа.
- Изменение правил расчета — только через ADR и golden-тесты (AGENTS.md §2).
