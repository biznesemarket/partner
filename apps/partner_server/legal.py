"""Юридический модуль (M7, legal-module.md): опциональный блок поверх учета.

Принцип модуля (legal-module.md §1): продукт полностью работает при
legal.enabled: false — учет, дайджесты, споры и снапшоты ни от чего
юридического не зависят. Юридические обещания живут в договорах, не в коде
(§5, красная линия): код знает только «расчетную долю / долю-баллы».

M7.1 снапшоты долей: раз в месяц итоги закрепляются — payload_json +
sha256 (canonical JSON) + git-якорь. Снапшот append-only: повтор за тот
же месяц с измененными числами — конфликт snapshot_conflict.
M7.2 экспорт юристу: один bundle — политика без персональных данных,
снапшоты, гейты, вопросы юристу, шаблоны документов (M7.4, редакционные,
без логики).
M7.3 гейты G-01…G-08: включение legal.enabled: true возможно только при
всех снятых гейтах; иначе отказ с перечнем незакрытых.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
from datetime import datetime, timezone
from decimal import Decimal

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, service

DEFAULT_POOL_PCT = 30.0  # динамический пул долей (equity-model.md)

# ---------- M7.3: гейты G-01…G-08 (legal-module.md §4) ----------

GATES: dict[str, dict] = {
    "G-01": {"subject": "Денежная часть вознаграждения (cash_share_pct)", "priority": "P0"},
    "G-02": {"subject": "Конструкция обещания доли (фантомная доля в договоре)", "priority": "P0"},
    "G-03": {"subject": "Публичные формулировки продукта и сайта", "priority": "P1"},
    "G-04": {"subject": "Хранение и защита данных", "priority": "P0"},
    "G-05": {"subject": "Договорная база участника (договор + CLA + NDA)", "priority": "P0"},
    "G-06": {"subject": "Налоги (НДФЛ, взносы, УСН-ограничения)", "priority": "P0"},
    "G-07": {"subject": "Персональные данные и трансграничная передача", "priority": "P0"},
    "G-08": {"subject": "Пакет фиксации (создание нового ООО по снапшоту)", "priority": "P2"},
}

# Вопросы юристу — постоянный список пакета экспорта (legal-module.md §7)
QUESTIONS_TO_LAWYER = [
    "Подтвердите конструкцию фантомной доли (доля-баллы) в договоре участника: "
    "не является ли она обещанием доли в ООО до фиксации (красная линия §5)?",
    "Формулировки денежной части: cash_share_pct выплачивается как гонорар "
    "или как часть вклада — какой договор нужен для каждой трактовки (G-01)?",
    "Налоговый режим начислений времени × ставка: НДФЛ и взносы при "
    "выплате cash, момент признания для доли-баллов (G-06)?",
    "Достаточно ли CLA до первого принятого вклада для ст. 1296 ГК РФ "
    "(отчуждение исключительных прав заказчику) (G-05, §6)?",
    "Порядок фиксации по месячному снапшоту при создании ООО: договор "
    "участников / корпоративный договор / опционные соглашения — какие из "
    "них обязательны в нашей юрисдикции (G-08)?",
    "Требуется ли уведомление Роскомнадзора и допустима ли трансграничная "
    "передача при текущей схеме хранения (G-07)?",
]


class LegalError(service.DomainError):
    """Ошибка юридического модуля — тот же контракт, что у сервисных ошибок."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_state() -> dict:
    """Свежее состояние: модуль выключен, все гейты открыты."""
    return {
        "legal": {"enabled": False, "enabled_at": None, "enabled_by": None},
        "gates": {
            gid: {"status": "open", "evidence": "", "actor": None, "updated_at": None}
            for gid in GATES
        },
    }


def load_state(path: pathlib.Path) -> dict:
    """Чтение состояния; отсутствующий или битый файл = состояние по умолчанию."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return default_state()
    if not isinstance(data, dict):
        return default_state()
    state = default_state()
    legal = data.get("legal") or {}
    state["legal"].update(
        {k: legal.get(k) for k in ("enabled", "enabled_at", "enabled_by")}
    )
    gates_in = data.get("gates") or {}
    for gid, entry in state["gates"].items():
        src = gates_in.get(gid) or {}
        entry.update(
            {k: src.get(k) for k in ("status", "evidence", "actor", "updated_at")}
        )
    return state


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(state, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )


def gate_report(state: dict) -> list[dict]:
    """Отчет по гейтам для API/CLI: предмет и приоритет — из GATES."""
    report = []
    for gid, meta in GATES.items():
        entry = state["gates"].get(gid, {})
        report.append(
            {
                "gate_id": gid,
                "subject": meta["subject"],
                "priority": meta["priority"],
                "status": entry.get("status") or "open",
                "evidence": entry.get("evidence") or "",
                "actor": entry.get("actor"),
                "updated_at": entry.get("updated_at"),
            }
        )
    return report


def unclosed_gates(state: dict) -> list[str]:
    """Гейты со статусом != done — включение модуля заблокировано."""
    return sorted(gid for gid in GATES if state["gates"].get(gid, {}).get("status") != "done")


def set_gate(
    path: pathlib.Path,
    gate_id: str,
    *,
    status: str,
    evidence: str,
    actor: str,
) -> dict:
    """Юрист снимает (или возвращает) гейт. Статусы: open | done."""
    if gate_id not in GATES:
        raise LegalError("unknown_gate", f"гейт {gate_id!r} неизвестен — ожидается G-01…G-08")
    if status not in ("open", "done"):
        raise LegalError("bad_gate_status", "status должен быть open или done")
    if status == "done" and len(evidence) < 5:
        raise LegalError(
            "gate_evidence_required",
            "для снятия гейта нужно описание основания (≥5 символов): документ, дата, подписант",
        )
    state = load_state(path)
    state["gates"][gate_id] = {
        "status": status,
        "evidence": evidence,
        "actor": actor,
        "updated_at": _now_iso(),
    }
    save_state(path, state)
    return {"gate_id": gate_id, **state["gates"][gate_id], "subject": GATES[gate_id]["subject"]}


def enable(path: pathlib.Path) -> dict:
    """Включение legal.enabled: true — только при всех снятых гейтах (M7.3).

    Иначе LegalError legal_gates_not_cleared с перечнем незакрытых гейтов.
    """
    state = load_state(path)
    unclosed = unclosed_gates(state)
    if state["legal"].get("enabled"):
        return {"status": "duplicate", "enabled": True, "unclosed": unclosed}
    if unclosed:
        raise LegalError(
            "legal_gates_not_cleared",
            "юридический модуль включить нельзя: не сняты гейты " + ", ".join(unclosed)
            + " — их снимает юрист до включения (legal-module.md §4)",
        )
    state["legal"]["enabled"] = True
    state["legal"]["enabled_at"] = _now_iso()
    state["legal"]["enabled_by"] = "lawyer"
    save_state(path, state)
    return {"status": "enabled", "enabled": True, "enabled_at": state["legal"]["enabled_at"], "unclosed": []}


# ---------- M7.1: месячный снапшот долей ----------


def _git_anchor() -> str | None:
    """Якорь в git: короткий SHA текущего HEAD; вне репозитория — None."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — вне репозитория якоря нет
        return None


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def build_month_snapshot(
    sess: Session,
    project: models.Project,
    month: str,
    *,
    actor: str,
) -> dict:
    """Месячный снапшот: вклад ₽ и расчетные доли всех участников (M7.1).

    Числа только из day_records (закрытые дни, append-only) и adjustments —
    воспроизводимы из БД, как в дайджестах M6. sha256 считается по canonical
    JSON payload (created_at и git_ref в хеш не входят). Повторный вызов:
    тот же sha — duplicate; изменившиеся числа — snapshot_conflict (снапшот
    иммутабелен; правки — только новый месяц).
    """
    if not (len(month) == 7 and month[4] == "-" and month[5:].isdigit()):
        raise LegalError("bad_month", f"ожидается формат YYYY-MM, получено {month!r}")

    rows = sess.execute(
        select(models.DayRecord)
        .join(models.Participant, models.DayRecord.participant_id == models.Participant.participant_id)
        .where(models.Participant.tenant_id == project.tenant_id)
        .where(models.DayRecord.project_id == project.project_id)
        .where(models.DayRecord.date.like(f"{month}%"))
        .where(models.DayRecord.closed == 1)
        .order_by(models.DayRecord.date)
    ).scalars().all()
    if not rows:
        raise LegalError(
            "no_days", f"за {month} нет закрытых дней — снапшот строится по закрытым дням"
        )

    day_ids = [r.day_id for r in rows]
    adj_delta: dict[str, int] = {}
    for a in sess.scalars(select(models.Adjustment).where(models.Adjustment.day_id.in_(day_ids))).all():
        adj_delta[a.day_id] = adj_delta.get(a.day_id, 0) + a.delta_cents

    participants: dict[str, dict] = {}
    for r in rows:
        p = participants.setdefault(
            r.participant_id,
            {"participant_id": r.participant_id, "days": 0, "paid_seconds": 0,
             "amount_cents": 0, "cash_cents": 0, "equity_cents": 0,
             "adjustments_delta_cents": 0, "share_bp": Decimal(0)},
        )
        p["days"] += 1
        p["paid_seconds"] += r.paid_seconds
        p["amount_cents"] += r.amount_cents
        p["cash_cents"] += r.cash_cents
        p["equity_cents"] += r.equity_cents
        p["adjustments_delta_cents"] += adj_delta.get(r.day_id, 0)
        p["share_bp"] += Decimal(r.share_bp or 0)
    for p in participants.values():
        p["share_bp"] = str(p["share_bp"].quantize(Decimal("0.0001")))
        p["share_pct"] = str((Decimal(p["share_bp"]) / 100).quantize(Decimal("0.0001")))
        p["amount_rub"] = str(Decimal(p["amount_cents"]) / 100)
        p["effective_amount_cents"] = p["amount_cents"] + p["adjustments_delta_cents"]

    totals_paid = sum(r.paid_seconds for r in rows)
    totals_amount = sum(r.amount_cents for r in rows)
    totals_share_bp = sum(Decimal(r.share_bp or 0) for r in rows).quantize(Decimal("0.0001"))

    pol, _, params = service.load_policy(sess, project.project_id)
    pool_pct = Decimal(str(params.get("equity_pool_pct", DEFAULT_POOL_PCT)))
    pool_bp = (pool_pct * Decimal(100)).quantize(Decimal("0.0001"))
    remainder_bp = (pool_bp - totals_share_bp).quantize(Decimal("0.0001"))

    payload = {
        "kind": "month_snapshot",
        "project_id": project.project_id,
        "tenant_id": project.tenant_id,
        "month": month,
        "tz": rows[-1].tz,
        "policy_version": pol.policy_version,
        "policy_sha256": pol.sha256,
        "pool_bp": str(pool_bp),
        "accrued_bp_month": str(totals_share_bp),
        "pool_remainder_bp": str(remainder_bp),
        "participants": [participants[k] for k in sorted(participants)],
        "totals": {
            "days": len(rows),
            "paid_seconds": totals_paid,
            "paid_hours": str(Decimal(totals_paid) / 3600),
            "amount_cents": totals_amount,
            "amount_rub": str(Decimal(totals_amount) / 100),
            "share_bp": str(totals_share_bp),
        },
    }
    sha = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    snapshot_id = f"snap-{project.project_id}-{month}"

    existing = sess.get(models.Snapshot, snapshot_id)
    if existing is not None:
        if existing.sha256 == sha:
            return {
                "status": "duplicate", "snapshot_id": snapshot_id, "month": month,
                "sha256": sha, "git_ref": existing.git_ref,
            }
        raise LegalError(
            "snapshot_conflict",
            f"снапшот {snapshot_id} уже закреплен с другим содержимым (sha {existing.sha256[:12]}…) "
            "— снапшот иммутабелен; изменения месяца фиксируются следующим снапшотом",
        )

    sess.add(
        models.Snapshot(
            snapshot_id=snapshot_id,
            month=month,
            sha256=sha,
            git_ref=_git_anchor(),
            created_at=_now_iso(),
            payload_json=payload,
        )
    )
    sess.commit()
    return {
        "status": "created", "snapshot_id": snapshot_id, "month": month,
        "sha256": sha, "git_ref": sess.get(models.Snapshot, snapshot_id).git_ref,
        "participants": len(participants),
    }


def render_month_snapshot(payload: dict, sha256: str, git_ref: str | None) -> str:
    """Детерминированный текст снапшота — подписываемый лист экспорта юристу."""
    lines = [
        f"Снапшот долей {payload['month']} — проект {payload['project_id']} ({payload['tz']})",
        f"Политика: v{payload['policy_version']} (sha256 {payload['policy_sha256'][:12]}…)",
        f"Пул долей: {payload['pool_bp']} бп · начислено за месяц: {payload['accrued_bp_month']} бп · остаток: {payload['pool_remainder_bp']} бп",
        "",
        f"{'Участник':<20} {'Дней':>5} {'Часы':>10} {'Вклад, ₽':>12} {'Доля, %':>10}",
    ]
    for p in payload["participants"]:
        lines.append(
            f"{p['participant_id']:<20} {p['days']:>5} {Decimal(p['paid_seconds']) / 3600:>10} "
            f"{p['amount_rub']:>12} {p['share_pct']:>10}"
        )
    t = payload["totals"]
    lines += [
        "",
        f"Итого: {t['days']} дн., {t['paid_hours']} ч, {t['amount_rub']} ₽, доля {t['share_bp']} бп",
        "",
        "Примечание: это расчетная доля (доля-баллы) динамического пула, а не доля в ООО; "
        "фиксация — по процедуре legal-module.md §2.4 после снятия гейтов.",
        "",
        f"sha256 снапшота: {sha256}",
        f"git-якорь: {git_ref or '—'}",
    ]
    return "\n".join(lines)


def snapshot_markdown(payload: dict, sha256: str, git_ref: str | None) -> str:
    """Подписываемый лист снапшота: заголовок + текст + место подписи."""
    return (
        "# Снапшот долей (подписываемый экспорт)\n\n"
        + render_month_snapshot(payload, sha256, git_ref)
        + "\n\n---\n\n"
        "Подпись директора: ____________________  Дата: ____________\n"
        "Подпись юриста (гейты сняты): ____________________  Дата: ____________\n"
    )


# ---------- M7.4: шаблоны фиксации (редакционные, без логики) ----------

# Красная линия legal-module.md §5: до фиксации — только «расчетная доля /
# доля-баллы»; обещание доли в ООО появляется в договоре после снятия гейтов.

PARTICIPANT_AGREEMENT_MD = """# Договор участников (шаблон, редакционный — M7.4)

> Шаблон без юридической силы. Заполняется юристом после снятия гейтов
> G-01, G-02, G-05, G-06 (legal-module.md §4). Данные сторон — в закрытом
> контуре, в репозиторий проекта не попадают (§6).

Стороны: {{organization}} ({{legal_entity}}, {{jurisdiction}}) и участник {{participant}}.

1. Предмет. Участник осуществляет вклад времени (и, при согласии, денежных
   средств) в проект {{project}}. Вклад времени измеряется системой учета
   «Партнер»: активное время человека, время агента под наблюдением,
   автономное время агента (с коэффициентами политики {{policy_ref}}).
2. Вознаграждение — денежная часть. {{cash_share_pct}} % суммы дня
   выплачивается деньгами {{payment_terms}}. Налоги удерживаются
   {{tax_terms}} (гейт G-06).
3. Вознаграждение — доля-баллы. Остальная часть дня увеличивает
   расчетную долю участника в динамическом пуле {{pool_pct}} % оценки
   проекта. До фиксации расчетная доля не является долей в уставном
   капитале и не дает корпоративных прав (гейт G-02).
4. Фиксация. По месячному снапшоту (sha256 указан в снапшоте) стороны
   при создании юридического лица или ином событии фиксации оформляют:
   договор участников / корпоративный договор / опционные соглашения.
   Порядок и сроки — {{fixation_terms}}.
5. Исключительные права. Результаты работ отчуждаются заказчику
   (ст. 1296 ГК РФ); соглашение об отчуждении (CLA) подписано до первого
   принятого вклада (гейт G-05).
6. Прочие условия: конфиденциальность, персональные данные (гейт G-07),
   статус good/bad leaver и перерасчет пула — по приложению
   {{appendix_ref}}.

Подписи: ________________
"""

OPTION_AGREEMENT_MD = """# Опционное соглашение (шаблон, редакционный — M7.4)

> Шаблон без юридической силы. Подключается юристом при событии фиксации
> (создание ООО / раунд / выход) по снятию гейта G-08.

Стороны: {{legal_entity}} и участник {{participant}}.

1. Организация предоставляет участнику право (опцион) требовать оформления
   долей/акций пропорционально расчетной доле на дату опорного снапшота
   {{snapshot_month}} (sha256 {{snapshot_sha256}}).
2. Опорным значением считается расчетная доля участника из снапшота
   динамического пула; переоценки после опорной даты — по
   {{repricing_terms}}.
3. Условия исполнения: {{exercise_terms}}. Порядок vesting: 48 мес.,
   клифф 12 мес. (если стороны не согласовали иное).
4. Налоговые последствия исполнения — {{tax_terms}} (гейт G-06).

Подписи: ________________
"""

LOAN_AGREEMENT_MD = """# Соглашение о займе (шаблон, редакционный — M7.4)

> Альтернативная конструкция фиксации денежного вклада партнера:
> вклад оформляется займом организации (legal-module.md §2.1), доля-баллы
> не образуются. Шаблон без юридической силы; гейт G-01.

Стороны: {{participant}} (займодавец) и {{legal_entity}} (заемщик).

1. Сумма займа: {{loan_amount}} ₽. Передача — {{transfer_terms}}.
2. Проценты: {{interest_terms}}. Срок: {{loan_term}}.
3. Погашение: денежными средствами; зачет встречных требований — только
   по письменному соглашению сторон.
4. Займ не является вкладом в пул долей и не увеличивает расчетную долю,
   если стороны прямо не указали иное в приложении {{appendix_ref}}.

Подписи: ________________
"""

NEWCO_FIXATION_MD = """# Пакет фиксации по снапшоту (чек-лист, редакционный — M7.4)

> Создание нового ООО по месячному снапшоту (legal-module.md §2.4, C6).
> Заполняется юристом; гейт G-08 (P2). Шаблон без юридической силы.

Опорный снапшот: {{snapshot_month}}, sha256 {{snapshot_sha256}},
git-якорь {{snapshot_git_ref}}.

1. [ ] Снапшот подписан директором и участниками (листы экспорта).
2. [ ] Гейты G-01…G-07 сняты (гейт-отчет в пакете юристу), G-08 — в работе.
3. [ ] Создано юридическое лицо: {{legal_entity}}, {{jurisdiction}}.
4. [ ] Договор участников / корпоративный договор — распределение долей
       по опорному снапшоту; пол основателя {{founder_floor_pct}} %
       сохранен.
5. [ ] Опционные соглашения для участников, чья фиксация отложена.
6. [ ] Перечисление долей не нарушает требования закона к обществам
       в юрисдикции {{jurisdiction}}.
7. [ ] Архив пакета юристу (export-legal) сохранен в закрытом контуре;
       персональные данные в git не попали.
"""

LEGAL_TEMPLATES: dict[str, str] = {
    "templates/participant-agreement.md": PARTICIPANT_AGREEMENT_MD,
    "templates/option-agreement.md": OPTION_AGREEMENT_MD,
    "templates/loan-agreement.md": LOAN_AGREEMENT_MD,
    "templates/newco-fixation-checklist.md": NEWCO_FIXATION_MD,
}


# ---------- M7.2: экспорт юристу ----------


def _policy_public(sess: Session, project: models.Project) -> dict:
    """Политика без персональных данных: параметры, версии, sha256."""
    pol, _, params = service.load_policy(sess, project.project_id)
    return {
        "policy_id": pol.policy_id,
        "policy_version": pol.policy_version,
        "sha256": pol.sha256,
        "created_at": pol.created_at,
        "params": params,
    }


def build_export_bundle(
    sess: Session,
    project: models.Project,
    *,
    legal_file: pathlib.Path,
) -> dict:
    """Пакет юристу (legal-module.md §7): политика, снапшоты, гейты,
    вопросы юристу, шаблоны M7.4, manifest с sha256 (подписываемый экспорт).

    Персональных данных нет: display_name и контакты не включаются —
    только participant_id, числа учета и параметры политики.
    """
    state = load_state(legal_file)
    snaps = [
        s for s in sess.scalars(select(models.Snapshot)).all()
        if s.snapshot_id.startswith(f"snap-{project.project_id}-")
    ]
    snaps.sort(key=lambda s: s.month)
    snapshots_out = [
        {
            "snapshot_id": s.snapshot_id,
            "month": s.month,
            "sha256": s.sha256,
            "git_ref": s.git_ref,
            "created_at": s.created_at,
            "payload": s.payload_json,
            # Подписываемый лист снапшота (M7.1) — CLI раскладывает его
            # в snapshots/<month>.md
            "text": snapshot_markdown(s.payload_json, s.sha256, s.git_ref),
        }
        for s in snaps
    ]
    gates = gate_report(state)
    bundle_core = {
        "policy": _policy_public(sess, project),
        "snapshots": snapshots_out,
        "gates": gates,
        "legal_enabled": bool(state["legal"].get("enabled")),
    }
    manifest_sha = hashlib.sha256(_canonical(bundle_core).encode("utf-8")).hexdigest()
    return {
        **bundle_core,
        "questions_to_lawyer": QUESTIONS_TO_LAWYER,
        "templates": dict(LEGAL_TEMPLATES),
        "manifest": {
            "kind": "partner-legal-export",
            "project_id": project.project_id,
            "bundle_sha256": manifest_sha,
            "generated_at": _now_iso(),
            "note": (
                "Пакет предназначен для отправки юристу (legal-module.md §7). "
                "Персональные данные не включены; полные тексты договоров "
                "подключаются как шаблоны M7.4. Подписывается: manifest "
                "(bundle_sha256) и листы снапшотов."
            ),
        },
    }

