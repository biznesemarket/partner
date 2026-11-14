# Схема политики проекта (policy.yaml)

| Поле | Значение |
|---|---|
| Код | POLICY-SCHEMA |
| Версия | 1.0 |
| Дата | 2026-11-13 |
| Связанные | policy.example.yaml, tools/policy_lint.py, onboarding-wizard, versioning |

## 1. Категории переменных

Все переменные политики делятся на четыре категории:

| Категория | Кто задает | Изменение |
|---|---|---|
| Обязательные при настройке | директор, мастер `partner init` | с момента ввода, через версии |
| Опциональные при настройке | директор или юрист, можно пропустить | позже, в любой момент |
| Системные | дефолт продукта | через ADR продукта |
| Юридические | юрист после гейтов | через включение `legal.enabled` |

Каждая переменная в схеме размечается `x-wizard`-метаданными — мастер
вопросов `partner init` генерируется из схемы, а не пишется вручную:

```yaml
daily_cap_hours:
  type: number
  default: 12
  x-wizard:
    ask: mandatory            # mandatory | optional | system
    question: "Дневной кап оплачиваемых часов?"
    range: [4, 16]
    hint: "Рекомендация: 10 или 12. Защита от накрутки 24 часов в сутки."
```

## 2. Блок meta

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| schema_version | string | 1.0 | Версия схемы политики |
| policy_version | int | 1 | Номер версии политики; растет с каждым коммитом |
| tenant_id | string | — | Идентификатор организации |
| project_id | string | — | Идентификатор проекта |
| approved_by | string | — | Кто утвердил политику |
| effective_from | datetime | — | Момент вступления в силу |

## 3. Блок project

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| name | string | — | Название проекта |
| forge | enum | github | github \| forgejo \| gitea |
| repo | string | — | owner/name репозитория |
| timezone | string | Europe/Moscow | Пояс закрытия дня (спрашивается) |
| default_branch | string | main | Ветка по умолчанию |

## 4. Блок economics

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| valuation_rub | int | 10000000 | Оценка проекта 100% (спрашивается, версонируется) |
| rate_rub_per_hour | int | 500 | Ставка часа с ИИ (спрашивается, версонируется) |
| currency | string | RUB | Валюта (спрашивается) |
| pool_pct | number | 30 | Пул участников, % от оценки (спрашивается) |
| founder_floor_pct | number | 70 | Пол основателя, % |
| cash_share_pct | number | 0 | Денежная часть начисления, % |
| cash_payment_schedule | enum | monthly | Периодичность выплаты денежной части |
| revaluation_notice_days | int | 3 | Предупреждение о переоценке, дней |
| pool_info_pct | number | 80 | Порог «пул близок к лимиту» |
| pool_warn_pct | number | 90 | Порог «готовится переоценка» |

## 5. Блок time

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| mode | enum | time_based | time_based \| fixed_price \| hybrid |
| heartbeat_interval_sec | int | 30 | Частота событий активности |
| active_window_sec | int | 120 | Окно активного класса |
| presence_window_sec | int | 600 | Окно присутствия человека |
| idle_timeout_sec | int | 300 | Порог простоя |
| max_clock_skew_sec | int | 300 | Допустимое расхождение часов |
| k_supervised | number | 1.0 | Коэффициент супервизии |
| k_autonomous | number | 0.2 | Коэффициент автономного времени (спрашивается; реком. 0,2–0,3) |
| daily_cap_hours | number | 12 | Дневной кап оплачиваемых часов (спрашивается; реком. 10 или 12) |
| autonomous_paid_cap_hours | number | 2.0 | Кап автономного класса в оплач. часах |
| k_quality_default | number | 1.0 | Коэффициент качества по умолчанию |
| manual_only_policy | enum | track_only | track_only \| bill \| ignore |
| require_task_link | bool | true | Сессия без задачи не оплачивается |

## 6. Блок day

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| close_at | string | 00:00 | Момент закрытия дня в поясе проекта |
| late_grace_hours | int | 24 | Грейс приема опоздавших событий |
| dispute_window_days | int | 3 | Окно оспаривания дня |
| digest_hour | int | 9 | Час утреннего дайджеста |
| digest_channels | list | [agent, email] | Каналы доставки дайджеста |

## 7. Блок vesting

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| enabled | bool | true | Вестинг включен |
| months | int | 48 | Полный срок вестинга, активных месяцев |
| cliff_months | int | 12 | Клифф |
| active_month_rule | enum | paid_hours | paid_hours \| amount (по режиму) |
| active_month_min_paid_hours | number | 20 | Порог активного месяца в оплач. часах |
| active_month_min_amount_rub | int | 10000 | Порог активного месяца при fixed_price |
| cash_vesting | bool | false | Вестить ли денежную долю |
| acceleration | enum | single_trigger_100 | Ускорение при смене контроля |
| control_change_threshold_pct | number | 50 | Порог смены контроля |
| inactive_pause_months | int | 3 | Пауза вестинга при неактивности |

## 8. Блок versions (история ставки и оценки)

```yaml
versions:
  rate:
    - effective_from: "2026-11-13T00:00:00+03:00"
      value: 500
      reason: "Стартовая ставка часа работы с ИИ-агентом"
      approved_by: founder
  valuation:
    - effective_from: "2026-11-13T00:00:00+03:00"
      value: 10000000
      reason: "Стартовая оценка проекта"
      approved_by: founder
      adr_ref: ADR-0016
```

Правила применения и проверки V-01…V-08 — см. versioning.md.

## 9. Блок identity

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| max_accounts_per_forge | int | 1 | Один аккаунт на платформу по умолчанию |
| require_proof | bool | true | Верификация владения аккаунтом |
| exception_approvers | list | [founder] | Кто разрешает исключения |
| exception_min_reason_len | int | 20 | Минимальная длина обоснования исключения |
| wip_scope | enum | person | Уровень агрегации параллельных сессий |

## 10. Блок quality

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| enabled | bool | false | Модуль коэффициента качества |
| k_default / k_min / k_max | number | 1.0 / 0.5 / 1.3 | Границы k_quality |
| requires_reason | bool | true | Обоснование обязательно |

## 11. Блок storage

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| raw_events_retention_days | int | 90 | Хранение сырых событий |
| git_anchor | enum | monthly | daily \| monthly — якорь итогов в git |
| anchor_repo_path | string | ledger/ | Каталог якорей в репозитории |

## 12. Блок legal (все — deferred)

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| enabled | bool | false | Юридический модуль включен |
| jurisdiction | string | null | Юрисдикция (опционально при настройке) |
| entity_name | string | null | Наименование юрлица (опционально) |
| entity_ids | object | null | ИНН/ОГРН и прочие реквизиты (опционально) |
| contract_template_ref | string | null | Ссылка на шаблон договора |
| newco_target_months | int | null | Целевой срок создания юрлица (опционально) |
| pd_consent_required | bool | true | Согласие на обработку ПДн |

Опциональные переменные можно пропустить при настройке и заполнить позже —
это явное требование постановки: «юридические данные задаются, но не
обязательны».

## 13. Блок security

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| require_signed_events | bool | true | Подпись батчей heartbeat |
| no_output_flag_hours | number | 3 | Порог флага no_output |
| night_autonomous_flag_hours | number | 6 | Порог флага night_autonomous_spike |
| synthetic_input_detection | bool | true | Детект синтетического ввода |

## 14. Что нельзя менять задним числом

- записи в `versions.rate` и `versions.valuation`;
- закрытые дневные записи (только компенсирующие записи);
- `policy_version` коммита (только forward-изменения).

## 15. Порядок применения политики

1. Валидация схемы и кросс-полевых проверок (`tools/policy_lint.py`).
2. Проверка версий V-01…V-08.
3. Фиксация `policy_version` и SHA-256 файла в дневные записи дня.
4. Только после этого политика считается действующей.

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 1.0 | 2026-11-13 | Полная схема переменных, x-wizard-разметка, категории |
