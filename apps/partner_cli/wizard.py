"""Мастер настройки `partner init` (итерация I3).

Генерирует policy.yaml и блок AGENTS.md из ответов директора/юриста.
Обязательные вопросы — 10, опциональные можно пропустить и заполнить позже.
Источник вопросов и дефолтов — x-wizard-разметка схемы политики
(docs/02-policy/policy-schema.md); здесь — рабочий скелет мастера.
"""
from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import dataclass

import yaml

BLOCK_BEGIN = "<!-- BEGIN PARTNER BLOCK v1.0"
BLOCK_END = "<!-- END PARTNER BLOCK v1.0 -->"

# Обязательные вопросы (onboarding-wizard.md §3)
MANDATORY = [
    ("tenant_name", "Название организации", None),
    ("project_name", "Название проекта", None),
    ("currency", "Валюта (RUB/USD/EUR)", "RUB"),
    ("valuation_rub", "Оценка проекта, 100% (целое число)", None),
    ("rate_rub_per_hour", "Ставка 1 часа работы с ИИ-агентом (целое число)", 500),
    ("timezone", "Часовой пояс закрытия дня (IANA)", "Europe/Moscow"),
    ("daily_cap_hours", "Дневной кап оплачиваемых часов (4–16)", 12),
    ("k_autonomous", "Коэффициент автономного времени агента (0–1)", 0.2),
    ("pool_pct", "Пул участников, % от оценки (1–99)", 30),
    ("approved_by", "Кто утверждает дни и корректировки", None),
]

# Опциональные вопросы — можно пропустить (Enter) и заполнить позже
OPTIONAL = [
    ("entity_name", "Реквизиты/название юрлица"),
    ("jurisdiction", "Юрисдикция"),
    ("newco_target_months", "Целевой срок создания юрлица, месяцев"),
]


@dataclass
class WizardAnswers:
    tenant_name: str
    project_name: str
    currency: str = "RUB"
    valuation_rub: int = 10_000_000
    rate_rub_per_hour: int = 500
    timezone: str = "Europe/Moscow"
    daily_cap_hours: float = 12
    k_autonomous: float = 0.2
    pool_pct: int = 30
    approved_by: str = "director"
    entity_name: str | None = None
    jurisdiction: str | None = None
    newco_target_months: int | None = None

    @classmethod
    def demo(cls) -> "WizardAnswers":
        """Обезличенные демо-ответы: 10 млн ₽ проект, 500 ₽/час, без ФИО."""
        return cls(
            tenant_name="ООО «Демо»",
            project_name="Партнер",
            currency="RUB",
            valuation_rub=10_000_000,
            rate_rub_per_hour=500,
            timezone="Europe/Moscow",
            daily_cap_hours=12,
            k_autonomous=0.2,
            pool_pct=30,
            approved_by="Директор (демо)",
        )


def validate(answers: WizardAnswers) -> list[str]:
    errors: list[str] = []
    if answers.valuation_rub <= 0:
        errors.append("Оценка проекта должна быть положительной")
    if answers.rate_rub_per_hour <= 0:
        errors.append("Ставка часа должна быть положительной")
    if not 4 <= answers.daily_cap_hours <= 16:
        errors.append("Дневной кап должен быть в диапазоне 4–16 часов")
    if not 0 <= answers.k_autonomous <= 1:
        errors.append("Коэффициент автономного времени должен быть в диапазоне 0–1")
    if not 1 <= answers.pool_pct <= 99:
        errors.append("Пул участников должен быть в диапазоне 1–99%")
    if answers.pool_pct + 70 > 100:  # founder_floor_pct дефолт 70
        errors.append("pool_pct + founder_floor_pct (70) не может превышать 100")
    return errors


def build_policy(a: WizardAnswers) -> dict:
    """Собирает policy.yaml-структуру с версиями ставки и оценки (versioning.md)."""
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).isoformat(timespec="seconds")
    founder_floor = 100 - a.pool_pct
    return {
        "meta": {
            "schema_version": "1.0",
            "policy_version": 1,
            "tenant_id": a.tenant_name,
            "project_id": a.project_name,
            "approved_by": a.approved_by,
            "effective_from": now,
        },
        "project": {
            "name": a.project_name,
            "forge": "github",
            "repo": "owner/repo",
            "timezone": a.timezone,
            "default_branch": "main",
        },
        "economics": {
            "valuation_rub": a.valuation_rub,
            "rate_rub_per_hour": a.rate_rub_per_hour,
            "currency": a.currency,
            "pool_pct": a.pool_pct,
            "founder_floor_pct": founder_floor,
            "cash_share_pct": 0,
            "cash_payment_schedule": "monthly",
            "revaluation_notice_days": 3,
            "pool_info_pct": 80,
            "pool_warn_pct": 90,
        },
        "time": {
            "mode": "time_based",
            "k_supervised": 1.0,
            "k_autonomous": a.k_autonomous,
            "daily_cap_hours": a.daily_cap_hours,
            "autonomous_paid_cap_hours": 2.0,
            "heartbeat_interval_sec": 30,
            "active_window_sec": 120,
            "presence_window_sec": 600,
            "idle_timeout_sec": 300,
            "max_clock_skew_sec": 300,
            "manual_only_policy": "track_only",
            "require_task_link": True,
        },
        "day": {
            "close_at": "00:00",
            "late_grace_hours": 24,
            "dispute_window_days": 3,
            "digest_hour": 9,
            "digest_channels": ["agent", "email"],
        },
        "vesting": {
            "enabled": True,
            "months": 48,
            "cliff_months": 12,
            "active_month_rule": "paid_hours",
            "active_month_min_paid_hours": 20,
            "active_month_min_amount_rub": 10000,
            "cash_vesting": False,
            "acceleration": "single_trigger_100",
            "control_change_threshold_pct": 50,
            "inactive_pause_months": 3,
        },
        "versions": {
            "rate": [
                {
                    "effective_from": now,
                    "value": a.rate_rub_per_hour,
                    "reason": "Стартовая ставка часа работы с ИИ-агентом",
                    "approved_by": a.approved_by,
                }
            ],
            "valuation": [
                {
                    "effective_from": now,
                    "value": a.valuation_rub,
                    "reason": "Стартовая оценка проекта",
                    "approved_by": a.approved_by,
                    "adr_ref": "ADR-0016",
                }
            ],
        },
        "identity": {
            "max_accounts_per_forge": 1,
            "require_proof": True,
            "exception_approvers": [a.approved_by],
            "exception_min_reason_len": 20,
            "wip_scope": "person",
        },
        "quality": {
            "enabled": False,
            "k_default": 1.0,
            "k_min": 0.5,
            "k_max": 1.3,
            "requires_reason": True,
        },
        "storage": {
            "raw_events_retention_days": 90,
            "git_anchor": "monthly",
            "anchor_repo_path": "ledger/",
        },
        "legal": {
            "enabled": bool(a.jurisdiction and a.entity_name),
            "jurisdiction": a.jurisdiction,
            "entity_name": a.entity_name,
            "entity_ids": None,
            "contract_template_ref": None,
            "newco_target_months": a.newco_target_months,
            "pd_consent_required": True,
        },
        "security": {
            "require_signed_events": True,
            "no_output_flag_hours": 3,
            "night_autonomous_flag_hours": 6,
            "synthetic_input_detection": True,
        },
    }


def render_policy_yaml(policy: dict) -> str:
    return yaml.safe_dump(
        policy, allow_unicode=True, sort_keys=False, default_flow_style=False
    )


def render_agents_block(policy: dict) -> str:
    eco = policy["economics"]
    tm = policy["time"]
    return (
        f"{BLOCK_BEGIN} — управляется системой «Партнер», не редактировать вручную -->\n\n"
        "# Правила учета «Партнер» (обязательны для ИИ-агента)\n\n"
        "Этот блок читается агентом первым. Время работы с агентом учитывается\n"
        "и конвертируется в вознаграждение и долю по правилам policy.yaml.\n\n"
        "| Параметр | Значение |\n|---|---|\n"
        f"| Оценка проекта | {eco['valuation_rub']:,} {eco['currency']} |\n"
        f"| Ставка часа с ИИ | {eco['rate_rub_per_hour']} {eco['currency']}/ч (версионируется) |\n"
        f"| Дневной кап | {tm['daily_cap_hours']} ч |\n"
        f"| Коэффициент автономного времени | {tm['k_autonomous']} |\n"
        f"| Пул участников | {eco['pool_pct']}% |\n"
        f"| Закрытие дня | 00:00 {policy['project']['timezone']} |\n\n"
        "1. Сессия без привязки к задаче/ветке не оплачивается (no_task_link).\n"
        "2. Автономная работа агента оплачивается с коэффициентом и капом 2,0 ч/сутки.\n"
        "3. Параллельные сессии не суммируются; простой > 5 минут не оплачивается.\n"
        "4. Не имитируйте активность: replay отменяет начисление дня.\n"
        "5. Отчеты: partner status | partner day | partner explain <date>.\n"
        "6. Спор по дню — в течение 3 суток. Ставка/оценка меняются только с даты.\n\n"
        f"{BLOCK_END}\n"
    )


def apply(
    answers: WizardAnswers,
    repo_root: pathlib.Path,
    dry_run: bool = False,
) -> list[pathlib.Path]:
    """Пишет policy.yaml и блок AGENTS.md. Возвращает список затронутых файлов."""
    errors = validate(answers)
    if errors:
        raise ValueError("; ".join(errors))
    policy = build_policy(answers)
    yaml_text = render_policy_yaml(policy)
    block = render_agents_block(policy)
    if dry_run:
        return []
    policy_path = repo_root / "policy.yaml"
    policy_path.write_text(yaml_text, encoding="utf-8")
    agents_path = repo_root / "AGENTS.md"
    if agents_path.exists():
        text = agents_path.read_text(encoding="utf-8")
        if BLOCK_BEGIN in text and BLOCK_END in text:
            pre = text.split(BLOCK_BEGIN)[0]
            post = text.split(BLOCK_END)[1]
            agents_path.write_text(pre + block.rstrip() + "\n" + post, encoding="utf-8")
        else:
            agents_path.write_text(text.rstrip() + "\n\n" + block, encoding="utf-8")
    else:
        agents_path.write_text(block, encoding="utf-8")
    return [policy_path, agents_path]
