# Agent Kit: плагин IDE и MCP-сервер учета

| Поле | Значение |
|---|---|
| Версия | 1.0 |
| Дата | 2026-11-14 |
| Назначение | Контракт компонента `partner-agent-kit`: сбор сессий и heartbeat в среде разработчика |
| Статус | реализовано в v1.1: `apps/partner_agent_kit` (CLI, офлайн-буфер, MCP stdio, VS Code-обертка) |
| Связанные | overview.md, connectors.md, time-accounting.md, threat-model.md, data-model.md |

## 1. Зачем

Сервер «Партнера» принимает время через `POST /v1/sessions` и
`POST /v1/heartbeats`, но в репозитории нет клиента, который делает это
из IDE/CLI. Agent Kit закрывает разрыв: разработчик работает с ИИ-агентом,
а кит автоматически относит время к задаче, ветке и человеку-владельцу.

Кит **не считает деньги** и не применяет политику: он только собирает
факты времени и передает их серверу. Расчет — golden-калькулятор
(`partner_cli.calc`) на сервере.

## 2. Состав

| Компонент | Назначение |
|---|---|
| `partner-agent` (CLI/daemon) | локальный процесс: сессия, heartbeat, офлайн-буфер, подпись |
| IDE-плагин (VS Code) | кнопки «старт/стоп», статус-бар, heartbeat; JetBrains — backlog |
| MCP-сервер | stdio JSON-RPC: инструменты старта, heartbeat, статуса, дня, спора |
| Локальный конфиг | `~/.config/partner/agent.yaml` (не в git): URL сервера, токен, устройство, участник |

## 3. Жизненный цикл сессии

```text
init → session.start → heartbeat (30 c) → session.pause/stop → flush → exit
```

1. При старте кит читает `partner.yaml`/`policy.yaml` проекта (tenant,
   project, ветка по умолчанию) и локальный конфиг.
2. `POST /v1/sessions` с `session_id`, `participant_id`, `project_id`,
   `task_ref`, `branch`, `agent_attached`, `started_at_utc`.
3. Каждые 30 секунд — `POST /v1/heartbeats` с монотонным `seq` и
   уникальным `event_id`; интервал `[start_utc, end_utc)` не пересекает
   границу суток (кит режет по локальной полуночи пояса проекта).
4. Класс времени определяется по сигналам:
   - `active_human` — есть ввод/фокус у человека;
   - `supervised_agent` — агент работает, человек присутствует;
   - `autonomous_agent` — агент работает, присутствия нет;
   - `idle` — простой дольше `idle_timeout_sec`.
5. При потере сети события буферизуются на диск и отправляются после
   восстановления в исходном порядке; `event_id` обеспечивает
   идемпотентность, `seq` — защиту от подмены.

## 4. Контракт heartbeat

```json
{
  "event_id": "evt-01J...",
  "session_id": "ses-01J...",
  "seq": 42,
  "cls": "supervised_agent",
  "start_utc": "2026-11-14T09:00:00+00:00",
  "end_utc": "2026-11-14T09:00:30+00:00",
  "signals": {
    "input_events": 12,
    "focus": 1,
    "agent_running": true,
    "tool_calls": 3
  },
  "received_at_utc": "2026-11-14T09:00:31+00:00"
}
```

Заголовки подписи устройства (threat-model §4):

```text
X-Partner-Device: dev-...
X-Partner-Sign: HMAC-SHA256(secret, event_id|session_id|seq|start_utc|end_utc)
```

Каноническая строка подписи задана один раз в
`apps/partner_cli/signing.py::canonical_heartbeat_line` и используется
и сервером, и китом. Секрет устройства выдается один раз при регистрации
(`POST /v1/devices`) и не попадает в git.

## 5. MCP-инструменты

| Инструмент | Действие | Ограничение |
|---|---|---|
| `partner_start_session` | старт сессии с задачей/веткой | `confirm=true` — подтверждение человека |
| `partner_heartbeat` | отправка одного интервала | не чаще 1 раза в 30 с |
| `partner_status` | текущая сессия и сегодняшние часы | только свой участник |
| `partner_day` | итог закрытого дня | только свой участник |
| `partner_explain` | раскладка начисления | только свой участник |
| `partner_dispute` | открыть спор дня | окно 3 суток, `confirm=true` |
| `partner_inbox` | входящие дайджесты/уведомления участника | только свой участник, `status`/`limit` |

MCP-сервер не получает доступ к коду клиента и не отправляет его во
внешние модели: инструменты работают только с числами учета.

## 6. Приватность и безопасность

- В git и на сервер не уходят фрагменты кода, промпты и содержимое файлов.
- Токен API и секрет устройства хранятся в защищенном хранилище ОС
  (keyring) или файле с правами 0600; в репозиторий не попадают.
- Ротация устройства: новое устройство + отзыв старого
  (`POST /v1/devices/{id}/revoke`).
- При `PARTNER_REQUIRE_DEVICE_SIGN=1` неподписанный heartbeat отклоняется.

## 7. Приемочные проверки

- [x] Сессия без `task_ref` дает `no_task_link` и не оплачивается
  (`tests/test_agent_kit.py::test_session_without_task_ref_is_not_paid`).
- [x] Повтор `event_id` не создает дубль; повтор `seq` → 422
  (`test_signed_heartbeat_accepted_and_replay_is_idempotent`,
  `test_duplicate_seq_rejected`).
- [x] Офлайн-буфер отправляет события в порядке `seq` после восстановления
  (`test_offline_buffer_replays_in_seq_order`).
- [x] Подпись устройства: валидная принята, поддельная → 403 + аудит
  (`test_wrong_device_secret_rejected` + серверные `tests/test_devices.py`).
- [x] Параллельные сессии одного участника не суммируются (union на сервере,
  T-04).
- [x] Автономный ночной прогон упирается в кап 2,0 оплач. ч (T-02).
- [x] Кит не отправляет код, промпты и персональные данные
  (`test_classification_and_signal_whitelist`: белый список сигналов).

## 8. Реализация v1.1

```text
partner-agent init --participant dev1 --task 42 --branch dev/dev1/42
partner-agent start --task 42 --branch dev/dev1/42
partner-agent run                 # heartbeat каждые 30 с
partner-agent heartbeat --input 3 --focus 1 --tool-calls 1
partner-agent stop | flush | status | day 2026-11-12
partner-agent mcp                 # MCP stdio для ИИ-агента
```

- Конфиг: `~/.config/partner/agent.yaml` (0600); секрет устройства — там же.
- Офлайн-буфер: `agent-buffer.jsonl` рядом с конфигом; состояние сессии —
  `agent-buffer.jsonl.state`. События не удаляются до успешной отправки;
  ретенция — до ручной очистки, длительный офлайн упирается в `grace_hours`
  сервера (`late_rejected`).
- `run` без IDE-плагина считает интервалы автономными; VS Code-обертка
  пишет счетчики активности в heartbeat и не передает текст/имена файлов.
- MCP: stdio, `initialize` → `2024-11-05`, инструменты требуют
  `confirm=true` для старта сессии и спора.
- Плагин VS Code: `apps/partner_agent_kit/vscode` (тонкая обертка CLI).
  JetBrains-плагин остается в backlog: контракт API уже готов.

## 9. Открытые вопросы

1. JetBrains-плагин и более точное определение присутствия (отладчик,
   фокус окна) — после пилота VS Code.
2. Отдельный локальный daemon не нужен: `partner-agent run` и плагин
   используют один и тот же CLI/конфиг.
3. MCP-транспорт: первым поддерживается stdio; HTTP-транспорт — при
   появлении клиентов без stdio.
4. Связывание задачи: вручную `task_ref` или по ветке; коннектор #2
   автоматически заполняет `task_ref` по соглашению `dev/<логин>/<тема>`.
5. Ретенция буфера: хранить до успешной отправки; при долгом офлайне
   события старше `grace_hours` получают `late_rejected` и не оплачиваются.

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 0.1 | 2026-11-14 | Первый draft контракта agent-kit |
| 1.0 | 2026-11-14 | Реализация v1.1: CLI, офлайн-буфер, подпись, MCP, VS Code |
