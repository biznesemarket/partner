# Runbook: эксплуатация сервера «Партнер»

| Поле | Значение |
|---|---|
| Версия | 1.0 |
| Дата | 2026-11-13 |
| Аудитория | администратор клиента, дежурный инженер |
| Связанные | installation.md, ADR-0017 (закрытие дня), ADR-0020 (бэкапы), threat-model.md |
| Статус | approved |

## 1. Назначение и границы

Runbook — инструкции для человека на дежурстве: что делать, когда алерт
сработал или система ведет себя не так, как описано в installation.md.
Принципы:

- **БД закрытых дней — не трогать руками.** Правки закрытых дней —
  только через споры и adjustments (M6); прямой UPDATE обнуляет
  sha256-цепочку и ломает снапшоты и юридический пакет (M7).
- **Сначала диагностика, потом действия.** Все команды безопасны
  (read-only или идемпотентны), кроме отмеченных ⚠.
- **Escalation:** админ клиента → поддержка вендора (в рамках подписки,
  ADR-0018) с приложением `docker compose logs server` и метрик.

## 2. Точки здоровья и ежедневная проверка

| Проверка | Команда / источник | Норма |
|---|---|---|
| Живость сервера | `curl http://…/healthz` | `{"status":"ok"}` |
| Метрики | `curl http://…/v1/metrics` | см. §3 |
| Планировщик | `docker compose logs server | grep dayclose` | закрытие после 00:00 пояса |
| Очередь доставки | метрика `partner_digest_pending` | 0 к 10:00 пояса |
| Свежесть бэкапа | `ls -lt deploy/backups/` | дамп младше 26 ч |

## 3. Метрики (/v1/metrics) и пороги алертов

| Метрика | Смысл | Порог алерта | Действие |
|---|---|---|---|
| `partner_heartbeats_last_hour` | пульс приема | 0 в рабочие часы > 30 мин | §4.1 |
| `partner_day_close_delay_seconds` | опоздание закрытия дня | > 900 сек | §4.2 |
| `partner_days_open_overdue` | незакрытые дни старше грейса | > 0 | §4.2 |
| `partner_digest_failed` | недоставленные дайджесты | > 0 | §4.3 |
| `partner_digest_pending` | очередь доставки | > 0 после digest_hour+15м | §4.3 |
| `partner_disputes_open` | открытые споры | информационная | директору |

Скрейп Prometheus: job `partner`, интервал 60 s; алерты — по таблице выше
(пример rules — в §6.2).

## 4. Типовые сбои и действия

### 4.1 Heartbeats перестали приходить

1. `curl /healthz` → если не ok: `docker compose logs server`, §4.5.
2. Если сервер жив — источник: агент-кит на машине разработчика.
   Проверить токен (`PARTNER_API_TOKEN` не ротировался? — 401 в логах)
   и сетевую достижимость. После устранения — данные подхватываются
   догоняющим закрытием (в порядке дат), ручных действий не нужно.
3. `partner_days_open_overdue > 0` — дни старше грейса все еще не
   закрыты: выполнить §4.2.

### 4.2 Закрытие дня не сработало / опоздало

1. `docker compose logs server | grep -i "закрыт\|dayclose"` — был ли прогон.
2. Разовая джоба (идемпотентно, повтор безопасен):
   `docker compose run --rm dayclose` — догоняющий прогон всех проектов;
   точечный день: `docker compose run --rm dayclose --date 2026-11-12`.
3. Причина задокументирована в журнале аудита (`GET /v1/audit`) —
   закрытие из джобы не отличается от штатного (тот же `close_day`).

### 4.3 Дайджесты не доставляются

1. `GET /v1/notifications?status=failed` — канал и last_error.
2. Webhook: проверить `PARTNER_WEBHOOK_URL` доступность (curl с хоста).
   SMTP: `PARTNER_SMTP_HOST/PORT/FROM/TO`, попытки — 3 (attempts),
   после чего `failed`; повтор — `docker compose restart server`
   (retry_pending выполняется при старте).
3. Доставка не влияет на расчеты: дайджест — витрина (ADR-0018).

### 4.4 Спор о дне

Не править БД руками. Цикл: `POST /v1/days/{date}/disputes` → директор
`POST /v1/disputes/{id}/resolve` в окне 3 дней → adjustment с ссылкой
на day_record. Просроченный спор закрывается сам (`expired`,
disputes.expire_overdue) — в дайджесте дня он был помечен заранее.

### 4.5 Сервер не поднимается

1. `docker compose logs server` — частое: БД недоступна (db здоров?
   `docker compose ps`), миграции не накатаны (`docker compose up migrate`).
2. Ошибка миграции — НЕ перезапускать по кругу; `rollback.sh` (§5.2).

### 4.6 Потеря данных (диск, VM, corruption)

1. Остановить запись: `docker compose stop server`.
2. Поднять инфраструктуру на чистой VM по installation.md §2.
3. Взять последний дамп: локальный (`deploy/backups/`) и офсайт-копию
   (ADR-0020 §3) — взять **самый свежий из двух площадок**.
4. Восстановить: `gunzip -c <дамп> | docker compose exec -T db pg_restore -d partner --no-owner --exit-on-error`.
5. Проверить: `docker compose exec backup /srv/scripts/restore_check.sh <дамп>`.
6. `docker compose up -d server` — догоняющее закрытие восстановит дни
   из heartbeats, попавших в дамп; дни после дампа — из повторной
   отправки агентами (grace_hours).
7. Сверить месячные снапшоты M7 с git-якорями и пакетом юристу
   (`GET /v1/legal`, `partner export-legal`).

Ориентир: RPO ≤ 24 ч, RTO ≤ 30 мин (ADR-0020).

### 4.7 Утечка токена / ключа

1. ⚠ Ротация: новый `PARTNER_API_TOKEN` в deploy/.env,
   `docker compose up -d server`; старый перестает действовать сразу.
2. Проверить журнал аудита: `GET /v1/audit?code=…` — аномальные
   cross_tenant_denied, анонимные записи.
3. Утечка лицензионного ключа — отзыв у вендора + перевыпуск (M9).

### 4.8 Обновление версии

Только по installation.md §4 (upgrade.sh: бэкап → миграция → рестарт →
healthz). Прямой `docker compose pull && up -d` без бэкапа запрещен
(ADR-0020 §5).

## 5. Процедуры

### 5.1 Репетиция восстановления (ежеквартально, после установки, перед релизом)

```
docker compose exec backup /srv/scripts/restore_check.sh
```

Ожидание: «РЕПЕТИЦИЯ ВОССТАНОВЛЕНИЯ ПРОЙДЕНА». Иначе — §4.6 и разбор
(`backup.log`). Отметка о репетиции — в журнал эксплуатации клиента
( release-чеклист v1.0: «восстановление отрепетировано», M8.3 DoD).

### 5.2 Откат обновления

```
deploy/scripts/rollback.sh <предыдущий тег> [дамп.dump.gz]
```

Схема откатывается только в крайнем случае: закрытые дни неизменны
(V-02/V-04), откат данных = восстановление из дампа (⚠ заменяет боевую БД).

### 5.3 Ручное закрытие месяца (если снапшот не закрыт 1-го числа)

```
docker compose run --rm dayclose            # сначала закрыть дни
# затем на машине администратора:
partner snapshot-close YYYY-MM
```

## 6. Приложения

### 6.1 Чек-лист дежурного (ежедневно)

- [ ] `/healthz` отвечает
- [ ] `partner_day_close_delay_seconds < 900`
- [ ] `partner_days_open_overdue == 0`
- [ ] `partner_digest_failed == 0`
- [ ] свежий дамп младше 26 ч

### 6.2 Пример алерт-правил Prometheus

```yaml
groups:
  - name: partner
    rules:
      - alert: PartnerDayCloseLate
        expr: partner_day_close_delay_seconds > 900
        for: 10m
      - alert: PartnerDaysOpenOverdue
        expr: partner_days_open_overdue > 0
        for: 30m
      - alert: PartnerDigestFailed
        expr: partner_digest_failed > 0
        for: 15m
      - alert: PartnerHeartbeatsStalled
        expr: partner_heartbeats_last_hour == 0
        for: 30m
```

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 1.0 | 2026-11-13 | Runbook M8.5: метрики, алерты, сбои, процедуры восстановления |
