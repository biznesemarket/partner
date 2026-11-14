# Спецификация калькулятора вкладов

| Поле | Значение |
|---|---|
| Код | POLICY-CALC |
| Версия | 1.0 |
| Дата | 2026-11-13 |
| Заменяет | B3-calculator-spec v0 (режим цены задачи перенесен в R2) |
| Связанные | time-accounting, equity-model, policy-schema, apps/partner_cli/calc.py |

## 1. Назначение и границы

Калькулятор превращает поток событий времени в дневные начисления по
правилам политики. Он не интерпретирует содержимое кода и не оценивает
«сложность»: единственная его функция — детерминированное применение
правил R1–R16. Реализация скелета: `apps/partner_cli/calc.py`
(целые копейки и секунды, никаких float в денежных значениях).

## 2. Точность и округление

| Объект | Единица | Правило |
|---|---|---|
| Интервалы времени | секунды | целые, разрезаются по границе суток и версии ставки |
| Часы | часы | секунды / 3600, без округления до применении капов |
| Денежные суммы | копейки | целые; ROUND_HALF_UP на последнем шаге формулы |
| Доля | процент | 4 знака после запятой; последний шаг после начисления |

## 3. Входные данные

| Вход | Источник |
|---|---|
| Сессии и heartbeats | agent-kit → сервер (прототип: SQLite `apps/partner_cli/schema.sql`) |
| policy.yaml | репозиторий проекта (валидация policy_lint) |
| versions.rate, versions.valuation | policy.yaml (versioning.md) |
| calendar дня | пояс проекта из policy.project.timezone |

## 4. Правила (машиночитаемые)

```text
R1 (time_based): amount_day = ROUND_HALF_UP(paid_hours × rate_active × k_quality, 2)
    paid_hours — по time-accounting §4 (классы, union, капы)
R2 (fixed_price): amount = ROUND_HALF_UP(base_rub × clamp(K), 2) — прежняя логика
    режима цены задачи; опциональный режим
R3 (union): пересекающиеся интервалы одного человека объединяются,
    не суммируются
R4 (split): интервалы режутся по границе суток и по смене версии ставки
R5 (caps): приоритет усечения active → supervised → autonomous;
    autonomous_paid ≤ autonomous_paid_cap_hours; total_paid ≤ daily_cap_hours
R6 (quality): k_quality применяется к дню целиком, только если модуль
    включен и есть обоснование директора
R7 (share): share_pct = amount_day / valuation_active × 100
R8 (cash): cash_day = ROUND_HALF_UP(amount_day × cash_share_pct / 100, 2);
    equity_day = amount_day − cash_day
R9 (no_task_link): интервалы сессий без привязки к задаче не оплачиваются
R10 (late): события позже late_grace_hours → late_rejected;
    в грейсе при закрытом дне → компенсирующая запись
R11 (replay): повтор event_id/нарушение seq → начисление дня отменяется
R12 (pending): при pool_used ≥ 100% — флаг pending, рубли копятся,
    доля не растет
R13 (vesting): fraction по активным месяцам (ADR-0019); отображается
    в дайджесте прогрессом «X из Y ч до активного месяца»
R14 (versioning): rate_active и valuation_active выбираются по versions[]
    на момент интервала/закрытия дня; в запись пишутся rate_version,
    valuation_version, policy_version
R15 (pool): при pool_used ≥ 100% дневная запись получает флаг pending
R16 (flags): флаги дня вычисляются после капов и попадают в дайджест
```

## 5. Псевдокод

```python
def calc_day(person, date, policy, sessions):
    intervals = classify(sessions, policy.time)        # классы по time-accounting §3
    intervals = union_parallel(intervals, person)      # R3
    intervals = split_by_midnight_and_rate(intervals)  # R4
    paid = apply_caps(intervals, policy.time)          # R5
    rate = active_version(policy.versions.rate, date)  # R14
    amount = round_half_up(paid * rate * k_quality)    # R1
    amount, flags = apply_fraud_flags(amount, sessions) # R16, R11
    share = amount / active_valuation(policy, date) * 100  # R7
    return DayRecord(paid, amount, share, flags,
                     rate_version=rate.version, policy_version=policy.version)
```

## 6. Выходы

| Выход | Потребитель |
|---|---|
| DayRecord (paid_hours, amount_rub, share_pct, flags, версии) | БД, дайджест, git-якорь |
| Месячный снапшот (агрегаты + SHA-256 входных дневных записей) | git, юрист (06-legal) |
| Объяснение `partner explain` | участник, директор, спор |

## 7. Приемочные тесты

Сценарии T-01…T-12 (time-accounting §13) и E-01…E-08 (equity-model §9)
являются golden-тестами: `pytest` проверяет, что реализация calc.py дает
в точности ожидаемые копейки. Тесты и документация меняются в одном PR —
расхождение недопустимо.

## 8. Вне рамок v1 (roadmap hooks)

- анализ качества кода как источник k_quality (только по явному решению
  директора, отключен по умолчанию);
- режим fixed_price (R2) — перенос прежнего калькулятора цен задач;
- мультипроектные организации: union интервалов между проектами.

## История изменений

| Версия | Дата | Изменение |
|---|---|---|
| 1.0 | 2026-11-13 | Правила R1–R16, привязка к тестам и скелету calc.py |
