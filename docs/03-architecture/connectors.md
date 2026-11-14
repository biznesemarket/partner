# Коннекторы git-платформ (GitHub / Forgejo / Gitea)

| Поле | Значение |
|---|---|
| Версия | 1.2 |
| Дата | 2026-11-14 |
| Адаптирует | D3-integration-github, D4-integration-forgejo, ADR-0006 (прежняя версия) |
| Статус | approved — v1.2: протоколы, подписи, backfill, обработка сбоев |
| Связанные | overview §5.5, agent-kit, data-model, threat-model, ADR-0016 |

## 1. Назначение и жесткая граница

Коннектор синхронизирует **факты платформы** (ветки, PR, задачи, метки)
в проект «Партнер». Он **не начисляет** вознаграждение: выход коннектора —
черновые факты в БД; расчет — только rules engine (наследие ADR-0006).

## 2. Контракт (общий для платформ)

| Элемент | GitHub | Forgejo / Gitea |
|---|---|---|
| Аутентификация API | GitHub App или PAT с минимальным scope | токен доступа |
| События | вебхуки push, pull_request, issues | вебхуки тех же типов |
| Привязка сессии | задача + ветка | задача + ветка |
| Формы споров | ISSUE_TEMPLATE contribution-dispute | шаблон issue |
| Лимиты | 5000 req/h (App 15000) | настройки инстанса |
| Приватность | только метаданные; код не скачивается коннектором | то же |

## 3. Телеметрия (новое после пивота)

Коннектор не является источником времени — время приходит из agent-kit
(heartbeat, подписанные батчи). Коннектор лишь **подтверждает связь
сессии с задачей/веткой** и поставляет факты коммитов/диффов для
антифрод-флагов (`no_output`) и дайджеста.

## 4. Матрица «событие → действие»

| Событие платформы | Действие коннектора |
|---|---|
| push в dev/* | факт `push` + `commit` на каждый sha; `branch_created`/`branch_deleted` по флагам события; привязка сессий |
| PR открыт/смержен | факт `pull_request`; при merge — `acceptance_draft` |
| issue создан (paid-task) | факт `issue` + факт `task` для метки `paid-task` |
| метка accepted | факт `acceptance` — триггер для снапшота месяца |
| issue dispute | факт `dispute_link` (окно спора проверяет сервер) |

## 5. Безопасность

- Секрет вебхука обязателен; подпись проверяется на сервере по сырому телу.
- Токены — минимальные скоупы (наследие ADR-0007), ротация по регламенту.
- Неверная подпись и отсутствие секрета фиксируются инцидентами в аудите.
- Сырой payload не сохраняется: в `connector_events` только действие,
  репозиторий и список типов фактов.

## 6. Реализация v1.2

### 6.1 Настройка вебхуков

| Платформа | Payload URL | События |
|---|---|---|
| GitHub | `https://<server>/v1/connectors/github/webhook` | `push`, `pull_request`, `issues` |
| Forgejo | `https://<server>/v1/connectors/forgejo/webhook` | `push`, `pull_request`, `issues` |
| Gitea | `https://<server>/v1/connectors/gitea/webhook` | `push`, `pull_request`, `issues` |

Порядок (одинаковый для всех платформ):

1. В настройках репозитория создать webhook: HTTP `POST`, content type
   `application/json`, SSL verification включен.
2. Указать секрет; он должен совпадать с `PARTNER_CONNECTOR_SECRET` или
   значением проекта в `PARTNER_CONNECTOR_SECRETS` (JSON `project → secret`).
3. Выбрать только события `push`, `pull_request`, `issues`; остальные типы
   принимаются, журналируются и не дают фактов.
4. Убедиться, что `projects.repo` = `owner/name`, `projects.forge` =
   `github | forgejo | gitea`. Если repo не привязан, сервер вернет
   `repo_not_mapped`; параметр `?project_id=<id>` задает проект явно и
   проверяется на совпадение repo/forge.
5. Отправить тестовую доставку и проверить `GET /v1/connectors/events`.

### 6.2 Заголовки, подпись и идемпотентность

| Платформа | Тип события | Доставка | Подпись |
|---|---|---|---|
| GitHub | `X-GitHub-Event` | `X-GitHub-Delivery` | `X-Hub-Signature-256: sha256=<hex>`; legacy `X-Hub-Signature: sha1=<hex>` |
| Forgejo | `X-Forgejo-Event` | `X-Forgejo-Delivery` | `X-Forgejo-Signature: <hex>` (допускается `sha256=<hex>`) |
| Gitea | `X-Gitea-Event` | `X-Gitea-Delivery` | `X-Gitea-Signature: <hex>` (допускается `sha256=<hex>`) |

Правила проверки:

- HMAC считается по **сырым байтам тела**, а не по разобранному JSON:
  `HMAC-SHA256(secret, raw_body)`; GitHub legacy — `HMAC-SHA1`.
- Сравнение — constant-time (`hmac.compare_digest`).
- Для Forgejo/Gitea дополнительно принимается `X-Hub-Signature-256`.
- `delivery_id` берется из заголовка доставки; если заголовка нет —
  `sha256(raw_body)[:64]`. `delivery_id` — первичный ключ `connector_events`,
  поэтому повторная доставка возвращает `{"status": "duplicate"}`.
- Факты тоже идемпотентны: `fact_id = sha256(delivery_id|kind|ref|branch)[:32]`,
  повторная материализация пропускается.

### 6.3 Протокол приема и ошибки

Успешный ответ (200) — после коммита события и фактов:

```json
{
  "status": "accepted",
  "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
  "facts": 4,
  "linked_sessions": [
    {"session_id": "ses-01J...", "branch": "dev/dev1/42", "task_ref": "42"}
  ]
}
```

Повторная доставка:

```json
{"status": "duplicate", "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958"}
```

| HTTP | Код | Когда |
|---|---|---|
| 400 | `invalid_json` | тело не JSON-объект |
| 401 | `webhook_signature_missing` | подпись отсутствует |
| 403 | `webhook_signature_invalid` | HMAC не сходится; инцидент в аудите |
| 403 | `repo_project_mismatch` / `forge_project_mismatch` | `project_id` не совпадает с repo/forge |
| 404 | `unknown_forge` | forge не из `github/forgejo/gitea` |
| 404 | `repo_not_mapped` | repo не привязан к проекту |
| 409 | `repo_ambiguous` | repo привязан к нескольким проектам |
| 503 | `connector_not_configured` | секрет вебхука не задан; инцидент в аудите |

Платформа повторяет доставку при 5xx/сетевом сбое; повтор безопасен
благодаря `delivery_id`. Сервер обрабатывает вебхук синхронно и отвечает
2xx только после записи события и фактов.

### 6.4 Факты (`repo_facts`)

| Событие | Факты |
|---|---|
| push | `push` (число коммитов, before/after, forced), `commit` на каждый sha, `branch_created`/`branch_deleted` |
| pull_request | `pull_request` (additions/deletions/changed_files, draft, base/head); `acceptance_draft` при merge |
| issues | `issue`; `task` для метки `paid-task`; `acceptance` для `accepted`; `dispute_link` для `dispute` |

Факты содержат только метаданные: sha, номера, ветки, метки, счетчики.
Сообщения коммитов, тела PR/issues, диффы и файлы не сохраняются.

- `GET /v1/connectors/facts?project_id=…&kind=…&branch=…&task_ref=…`
- `GET /v1/connectors/events?project_id=…`
- `POST /v1/connectors/{forge}/import` — backfill из API платформы.

### 6.5 Backfill через API

`partner-connector sync-repo` собирает **первую страницу** каждой коллекции
и отправляет один идемпотентный пакет импорта.

| Платформа | Запросы |
|---|---|
| GitHub | `GET /repos/{owner}/{repo}/branches?per_page=100`, `GET /pulls?state=all&per_page=100`, `GET /issues?state=all&per_page=100` |
| Forgejo / Gitea | `GET /api/v1/repos/{owner}/{repo}/branches?limit=100`, `GET /pulls?state=all&limit=100`, `GET /issues?state=all&type=issues&limit=100` |

Параметры клиента:

```text
partner-connector sync-repo --repo owner/name --project partner \
  --forge github --server-url https://partner.example \
  --forge-token <token платформы> --token <Bearer сервера>
```

- GitHub: base URL по умолчанию `https://api.github.com`, заголовок
  `Authorization: token <token>`; для GitHub Enterprise — `--api-url`.
- Forgejo/Gitea: `--api-url` обязателен (базовый URL инстанса), токен —
  `--forge-token` или `PARTNER_FORGE_TOKEN`.
- Токен сервера — `--token` или `PARTNER_API_TOKEN`.
- Нормализация: ветка → `branch`; PR → `pull_request`; issue → `issue` плюс
  факты по меткам `paid-task`/`accepted`/`dispute`; `task_ref` выводится из
  ветки по соглашению `dev/<логин>/<тема>`.
- Идемпотентность: если `delivery_id` не задан, используется
  `import-<sha256(canonical JSON payload)[:32]>`; повторный запуск с теми же
  фактами возвращает `duplicate`.
- Ответ импорта: `{status, delivery_id, facts, linked_sessions}`.

### 6.6 Сетевые сбои, повторы и лимиты

| Ситуация | Поведение v1.2 | Действие оператора |
|---|---|---|
| Платформа не доставила вебхук | сервер не знает о событии | redeliver в UI платформы или `import` backfill |
| Повтор доставки | `duplicate` по `delivery_id`, факты не дублируются | ничего |
| Таймаут API платформы при backfill | `ConnectorAPIError`, CLI завершается с кодом 1 | повторить `sync-repo`; импорт идемпотентен |
| Таймаут импорта на сервере | `SyncError`, CLI завершается с кодом 1 | повторить; повтор не создаст дублей |
| 403/429 от платформы (лимит) | ошибка API, без встроенного backoff | подождать лимит и повторить |
| Больше 100 веток/PR/issues | обрабатывается только первая страница | известное ограничение; повторить после ручной пагинации (backlog) |

Таймауты: API платформы — 20 с на запрос, импорт в сервер — 30 с.
Автоматических ретраев с backoff в v1.2 нет: повтор — идемпотентный ручной
запуск. GitHub: ~3 запроса на backfill при лимите 5000 req/h; для Forgejo
лимиты задаются инстансом.

### 6.7 Привязка сессий

Соглашение `dev/<логин>/<тема>` (development-process.md §1): тема ветки —
это `task_ref`. Коннектор заполняет **пустой** `task_ref` сессии с той же
веткой (`project_id` + `branch`) и **пустую** ветку у сессии с тем же
`task_ref`. Явно заданные значения не перезаписываются.

### 6.8 no_output

`no_output` ставится, если за день оплачено > 3 ч, а результата нет:
нет коммитов/PR с диффами, привязанных к ветке/задаче сессий участника,
и сумма `signals.tool_calls` равна нулю. Флаг не меняет начисление.

## 7. Приемочные проверки перед пилотом (⚠VERIFY)

- [x] Типы вебхуков и подпись на целевой платформе — контракт покрыт
  тестами `tests/test_connectors.py`.
- [x] Идемпотентность доставок и импорта — тесты duplicate/replay.
- [x] Права токена: только чтение метаданных и задач (`partner-connector`
  использует GET branches/pulls/issues).
- [ ] Лимиты API и пагинация для размеров клиентских команд — проверить на
  инстансе; в v1.2 обрабатывается первая страница (100 записей).
- [ ] Redeliver вебхука на реальном GitHub/Forgejo — проверить в пилоте.

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 1.0 | 2026-11-13 | Адаптация D3/D4: телеметрия из agent-kit, контракт фактов |
| 1.1 | 2026-11-14 | Реализация v1.1: вебхуки, факты, привязка сессий, no_output |
| 1.2 | 2026-11-14 | Протоколы v1.2: заголовки и подписи, коды ошибок, backfill, сбои, лимиты, ограничения |
