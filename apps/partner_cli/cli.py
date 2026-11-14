"""CLI «Партнер»: init, status, day, explain, snapshot, policy set.

Рабочий скелет (итерация I3). Полная серверная часть — итерация I4.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import zoneinfo
from decimal import Decimal

import typer
from rich.console import Console
from rich.table import Table

from . import calc, storage, wizard
from .console_io import configure_stdio

configure_stdio()

app = typer.Typer(help="Партнер — учет времени работы с ИИ-агентом и долей")
console = Console()

DEFAULT_DB = pathlib.Path("data/partner.sqlite3")
DEFAULT_POLICY = pathlib.Path("policy.yaml")


def _load_policy(path: pathlib.Path) -> dict:
    import yaml

    if not path.exists():
        console.print("[red]policy.yaml не найден. Сначала: partner init[/red]")
        raise typer.Exit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _policy_to_calc(pol: dict) -> calc.Policy:
    return calc.Policy(
        rate_rub_per_hour=int(pol["economics"]["rate_rub_per_hour"]),
        valuation_rub=int(pol["economics"]["valuation_rub"]),
        daily_cap_hours=float(pol["time"]["daily_cap_hours"]),
        autonomous_paid_cap_hours=float(pol["time"]["autonomous_paid_cap_hours"]),
        k_supervised=float(pol["time"]["k_supervised"]),
        k_autonomous=float(pol["time"]["k_autonomous"]),
        manual_only_policy=pol["time"].get("manual_only_policy", "track_only"),
        cash_share_pct=float(pol["economics"].get("cash_share_pct", 0)),
    )


def _demo_intervals() -> list[calc.Interval]:
    """Демо-день по сценарию T-01: active 4 ч, supervised 2 ч, autonomous 3 ч, idle 1 ч."""
    return [
        calc.Interval(10 * 3600, 14 * 3600, calc.CLASS_ACTIVE),
        calc.Interval(14 * 3600, 16 * 3600, calc.CLASS_SUPERVISED),
        calc.Interval(16 * 3600, 19 * 3600, calc.CLASS_AUTONOMOUS),
        calc.Interval(19 * 3600, 20 * 3600, calc.CLASS_IDLE),
    ]


@app.command()
def init(
    demo: bool = typer.Option(False, "--demo", help="Демо-ответы tenant #1 без вопросов"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Только предпросмотр"),
    root: pathlib.Path = typer.Option(pathlib.Path("."), help="Корень репозитория клиента"),
) -> None:
    """Мастер настройки: 10 обязательных вопросов, опциональные можно пропустить."""
    if demo:
        answers = wizard.WizardAnswers.demo()
    else:
        values = {}
        for key, question, default in wizard.MANDATORY:
            values[key] = typer.prompt(question, default=default)
        console.print("[dim]— Опционально: Enter — пропустить, заполните позже —[/dim]")
        for key, question in wizard.OPTIONAL:
            values[key] = typer.prompt(question, default="", hide_input=False)
        answers = wizard.WizardAnswers(
            tenant_name=str(values["tenant_name"]),
            project_name=str(values["project_name"]),
            currency=str(values["currency"]),
            valuation_rub=int(values["valuation_rub"]),
            rate_rub_per_hour=int(values["rate_rub_per_hour"]),
            timezone=str(values["timezone"]),
            daily_cap_hours=float(values["daily_cap_hours"]),
            k_autonomous=float(values["k_autonomous"]),
            pool_pct=int(values["pool_pct"]),
            approved_by=str(values["approved_by"]),
            entity_name=str(values.get("entity_name") or "") or None,
            jurisdiction=str(values.get("jurisdiction") or "") or None,
            newco_target_months=int(values["newco_target_months"])
            if str(values.get("newco_target_months") or "").strip().isdigit()
            else None,
        )
    # проверка пояса
    try:
        zoneinfo.ZoneInfo(answers.timezone)
    except Exception:
        console.print(f"[red]Неизвестный часовой пояс: {answers.timezone}[/red]")
        raise typer.Exit(1)

    policy = wizard.build_policy(answers)
    table = Table(title="Предпросмотр политики")
    table.add_column("Параметр")
    table.add_column("Значение")
    rows = [
        ("Организация / проект", f"{answers.tenant_name} / {answers.project_name}"),
        ("Оценка проекта", f"{answers.valuation_rub:,} {answers.currency}".replace(",", " ")),
        ("Ставка часа с ИИ", f"{answers.rate_rub_per_hour} {answers.currency}/ч"),
        ("Часовой пояс закрытия дня", answers.timezone),
        ("Дневной кап", f"{answers.daily_cap_hours} ч"),
        ("Коэффициент автономного времени", str(answers.k_autonomous)),
        ("Пул участников / пол основателя", f"{answers.pool_pct}% / {100 - answers.pool_pct}%"),
        ("Утверждающий дни", answers.approved_by),
        ("Юрлицо / юрисдикция", f"{answers.entity_name or '— (позже)'} / {answers.jurisdiction or '— (позже)'}"),
        ("legal.enabled", str(policy["legal"]["enabled"])),
    ]
    for r in rows:
        table.add_row(*r)
    console.print(table)
    if dry_run:
        console.print("[yellow]--dry-run: файлы не записаны[/yellow]")
        return
    files = wizard.apply(answers, root)
    console.print("[green]Записано:[/green] " + ", ".join(str(f) for f in files))
    console.print(
        "Дальше: закоммитьте как «policy v1.0», проверьте "
        "[bold]python tools/policy_lint.py policy.yaml[/bold] и [bold]pytest[/bold]"
    )


@app.command()
def status(
    db: pathlib.Path = typer.Option(DEFAULT_DB, help="Путь к SQLite прототипа"),
) -> None:
    """Текущая сессия и сегодняшний учет (демо-данные прототипа)."""
    pol = _load_policy(DEFAULT_POLICY)
    console.print(f"Проект: {pol['meta']['project_id']} · режим: {pol['time']['mode']}")
    console.print(
        f"Ставка: {pol['economics']['rate_rub_per_hour']} "
        f"{pol['economics']['currency']}/ч · оценка: "
        f"{pol['economics']['valuation_rub']:,} {pol['economics']['currency']}"
        .replace(",", " ")
    )
    console.print(
        f"Закрытие дня: {pol['day']['close_at']} {pol['project']['timezone']} · "
        f"дайджест в {pol['day']['digest_hour']}:00 · спор {pol['day']['dispute_window_days']} сут."
    )
    console.print("[dim]Серверная телеметрия появится в I4; демо-день: partner day --demo[/dim]")


@app.command()
def day(
    date: str = typer.Argument(None, help="Дата YYYY-MM-DD (по умолчанию вчера)"),
    demo: bool = typer.Option(False, "--demo", help="Посчитать демо-день T-01"),
    db: pathlib.Path = typer.Option(DEFAULT_DB),
) -> None:
    """Итог дня: оплачиваемые часы по классам, рубли и доля."""
    pol = _load_policy(DEFAULT_POLICY)
    calc_pol = _policy_to_calc(pol)
    tz_name = pol["project"]["timezone"]
    if date is None:
        date = (dt.datetime.now(zoneinfo.ZoneInfo(tz_name)) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    if not demo:
        console.print("[yellow]Дневные записи создаются сервером (I4). Демо: partner day --demo[/yellow]")
        raise typer.Exit(0)
    result = calc.calc_day(_demo_intervals(), calc_pol)
    _print_day(date, calc_pol, result)
    conn = storage.connect(db)
    sha = storage.save_day_record(
        conn, "p-demo", date, tz_name, result,
        rate_version=int(pol["meta"]["policy_version"]),
        valuation_version=int(pol["meta"]["policy_version"]),
        policy_version=int(pol["meta"]["policy_version"]),
        cash_share_pct=calc_pol.cash_share_pct,  # аудит-фикс F2b (R8)
    )
    console.print(f"[dim]Записано в {db} · sha256 дня: {sha[:16]}…[/dim]")


def _print_day(date: str, calc_pol: calc.Policy, result: calc.DayResult) -> None:
    console.print(f"[bold]День {date}[/bold]")
    for cls, hours_str in result.breakdown.items():
        console.print(f"  {cls}: {hours_str} ч")
    rub = result.amount_cents / 100
    console.print(
        f"Оплачено: {result.paid_hours} ч → {rub} {calc_pol.rate_rub_per_hour and 'RUB'} → "
        f"{result.share_pct}% проекта"
    )
    if result.flags:
        console.print(f"Флаги: {', '.join(result.flags)}")
    pool_budget = Decimal(calc_pol.valuation_rub) * Decimal(30) / 100
    day_rub = Decimal(result.amount_cents) / 100
    console.print(
        f"[dim]Пул (30%): {pool_budget:,} ₽ · использовано этим днем: "
        f"{day_rub / pool_budget * 100:.4f}%[/dim]".replace(",", " ")
    )


@app.command()
def explain(
    date: str = typer.Argument(..., help="Дата YYYY-MM-DD"),
    db: pathlib.Path = typer.Option(DEFAULT_DB),
) -> None:
    """Раскладка расчета дня по интервалам, классам, капам и ставке."""
    conn = storage.connect(db)
    row = conn.execute(
        "SELECT * FROM day_records WHERE date=?", (date,)
    ).fetchone()
    if row is None:
        console.print(f"[red]Дневная запись {date} не найдена[/red]")
        raise typer.Exit(1)
    table = Table(title=f"Раскладка дня {date}")
    table.add_column("Поле")
    table.add_column("Значение")
    for key in (
        "participant_id", "paid_seconds", "amount_cents", "cash_cents",
        "equity_cents", "share_bp", "rate_version", "valuation_version",
        "policy_version", "sha256_input",
    ):
        table.add_row(key, str(row[key]))
    console.print(table)
    flags = conn.execute("SELECT code FROM flags WHERE day_id=?", (row["day_id"],)).fetchall()
    if flags:
        console.print("Флаги: " + ", ".join(f["code"] for f in flags))


@app.command()
def snapshot(
    month: str = typer.Argument(..., help="Месяц YYYY-MM"),
    db: pathlib.Path = typer.Option(DEFAULT_DB),
) -> None:
    """Месячный итог: агрегаты по участникам и прогресс активного месяца."""
    conn = storage.connect(db)
    totals = storage.month_totals(conn, month)
    if not totals["participants"]:
        console.print(f"[yellow]Нет дневных записей за {month}[/yellow]")
        return
    table = Table(title=f"Снапшот {month}")
    table.add_column("Участник")
    table.add_column("Оплач. часы")
    table.add_column("Начислено, ₽")
    for p in totals["participants"]:
        table.add_row(p["participant_id"], str(p["paid_hours"]), f"{p['amount_rub']:.2f}")
    console.print(table)
    console.print(f"Итого за месяц: {totals['month_total_rub']:.2f} ₽")


policy_app = typer.Typer(help="Изменение параметров политики (только с даты)")
app.add_typer(policy_app, name="policy")


@policy_app.command("set")
def policy_set(
    key: str = typer.Argument(..., help="rate | valuation"),
    value: int = typer.Argument(..., help="Новое значение"),
    reason: str = typer.Option(..., "--reason", help="Обоснование (≥5 символов)"),
) -> None:
    """Новая версия ставки/оценки. Задним числом менять нельзя (V-04)."""
    pol = _load_policy(DEFAULT_POLICY)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).isoformat(timespec="seconds")
    if len(reason) < 5:
        console.print("[red]V-06: reason короче 5 символов[/red]")
        raise typer.Exit(1)
    if value <= 0:
        console.print("[red]V-03: value <= 0[/red]")
        raise typer.Exit(1)
    kind = "rate" if key == "rate" else "valuation"
    pol["versions"][kind].append(
        {
            "effective_from": now,
            "value": value,
            "reason": reason,
            "approved_by": pol["meta"]["approved_by"],
            **({"adr_ref": "ADR-0016"} if kind == "valuation" else {}),
        }
    )
    if kind == "rate":
        pol["economics"]["rate_rub_per_hour"] = value
    else:
        pol["economics"]["valuation_rub"] = value
    pol["meta"]["policy_version"] = int(pol["meta"]["policy_version"]) + 1
    DEFAULT_POLICY.write_text(
        wizard.render_policy_yaml(pol), encoding="utf-8"
    )
    console.print(
        f"[green]Версия {kind} добавлена ({now}). Прошлые начисления не меняются.[/green]\n"
        "Проверка: python tools/policy_lint.py policy.yaml"
    )


# ---------- M6: спор дня и дайджесты (через сервер, PARTNER_SERVER_URL) ----------


def _server_request(
    method: str, path: str, body: dict | None = None, server_url: str | None = None
) -> dict:
    """Вызов HTTP API сервера «Партнер» (M6). Ошибки домена — человекочитаемо."""
    import json
    import os
    import urllib.error
    import urllib.request

    base = server_url or os.getenv("PARTNER_SERVER_URL", "")
    if not base:
        console.print(
            "[red]PARTNER_SERVER_URL не задан. Спор дня и дайджесты работают "
            "через сервер: PARTNER_SERVER_URL=http://localhost:8000 partner dispute …[/red]"
        )
        raise typer.Exit(1)
    url = base.rstrip("/") + "/v1" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    token = os.getenv("PARTNER_API_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        console.print(f"[red]HTTP {err.code}: {detail}[/red]")
        raise typer.Exit(1)
    except urllib.error.URLError as err:
        console.print(f"[red]сервер недоступен ({base}): {err.reason}[/red]")
        raise typer.Exit(1)


@app.command()
def dispute(
    date: str = typer.Argument(..., help="Дата спорного дня YYYY-MM-DD"),
    reason: str = typer.Option(..., "--reason", help="Почему день спорный"),
    by: str = typer.Option(..., "--by", help="ID участника (разработчика)"),
    interval: list[str] = typer.Option([], "--interval", help="Спорный interval_id (можно несколько)"),
    project_id: str = typer.Option("partner", "--project"),
) -> None:
    """Спор дня: разработчик помечает закрытый день (окно 3 дня)."""
    result = _server_request(
        "POST",
        f"/days/{date}/disputes",
        {
            "participant_id": by,
            "opened_by": by,
            "reason": reason,
            "interval_ids": list(interval),
        },
    )
    console.print(
        f"[green]Спор {result['dispute_id']} открыт по дню {date}. "
        f"Решение директора до {result['window_deadline']}.[/green]"
    )


@app.command("dispute-decide")
def dispute_decide(
    dispute_id: str = typer.Argument(..., help="ID спора (dsp-…)"),
    decision: str = typer.Option(..., "--decision", help="approved | rejected"),
    delta_rub: float = typer.Option(0.0, "--delta-rub", help="Корректировка, ₽ (для approved)"),
    by: str = typer.Option(..., "--by", help="Кто решает (директор)"),
    comment: str = typer.Option("", "--comment", help="Обоснование решения"),
) -> None:
    """Решение директора по спору: approved → adjustment (append-only)."""
    result = _server_request(
        "POST",
        f"/disputes/{dispute_id}/resolve",
        {
            "decided_by": by,
            "decision": decision,
            "delta_cents": int(round(delta_rub * 100)) or None,
            "resolution": comment or None,
        },
    )
    adj = result.get("adjustment_id")
    console.print(
        f"[green]Спор {dispute_id}: {result['status']}."
        + (f" Корректировка применена: {adj} ({delta_rub:+.2f} ₽)." if adj else "")
        + "[/green]"
    )


@app.command()
def digest(
    what: str = typer.Argument(..., help="day | month"),
    key: str = typer.Argument(..., help="Дата YYYY-MM-DD или месяц YYYY-MM"),
    participant_id: str = typer.Option(..., "--participant"),
    project_id: str = typer.Option("partner", "--project"),
) -> None:
    """Дайджест дня/месяца: числа воспроизводимы из БД сервера."""
    if what == "day":
        result = _server_request("GET", f"/digest/day/{key}?participant_id={participant_id}")
    elif what == "month":
        result = _server_request("GET", f"/digest/month/{key}?participant_id={participant_id}")
    else:
        console.print("[red]what должен быть day или month[/red]")
        raise typer.Exit(1)
    console.print(result["text"])


# ---------- M7: юридический модуль (гейты, снапшоты, экспорт юристу) ----------


@app.command("legal-gates")
def legal_gates() -> None:
    """Отчет по гейтам G-01…G-08 и статусу юридического модуля."""
    result = _server_request("GET", "/legal")
    state = "включен" if result["enabled"] else "[yellow]выключен[/yellow]"
    console.print(f"Юридический модуль: {state}")
    table = Table(title="Гейты (все должны быть сняты юристом до включения)")
    table.add_column("Гейт")
    table.add_column("Приоритет")
    table.add_column("Статус")
    table.add_column("Предмет")
    for g in result["gates"]:
        status = "[green]done[/green]" if g["status"] == "done" else "[red]open[/red]"
        table.add_row(g["gate_id"], g["priority"], status, g["subject"])
    console.print(table)
    if result["unclosed_gates"]:
        console.print(f"[red]Не сняты: {', '.join(result['unclosed_gates'])}[/red]")


@app.command("legal-gate")
def legal_gate(
    gate_id: str = typer.Argument(..., help="G-01…G-08"),
    status: str = typer.Option(..., "--status", help="done | open"),
    evidence: str = typer.Option("", "--evidence", help="Основание: документ, дата, подписант"),
    by: str = typer.Option(..., "--by", help="Кто меняет (юрист)"),
) -> None:
    """Снять или вернуть гейт (done требует основания ≥5 символов)."""
    result = _server_request(
        "POST", f"/legal/gates/{gate_id}",
        {"status": status, "evidence": evidence, "actor": by},
    )
    console.print(
        f"[green]Гейт {result['gate_id']} → {result['status']}.[/green] "
        f"Предмет: {result['subject']}"
    )


@app.command("legal-enable")
def legal_enable(by: str = typer.Option(..., "--by", help="Кто включает (юрист)")) -> None:
    """Включить юридический модуль (legal.enabled: true) — только при снятых гейтах."""
    result = _server_request("POST", "/legal/enable", {"actor": by})
    if result["status"] == "duplicate":
        console.print("[yellow]Модуль уже включен.[/yellow]")
    else:
        console.print(f"[green]legal.enabled: true ({result['enabled_at']}).[/green]")


@app.command("snapshot-close")
def snapshot_close(
    month: str = typer.Argument(..., help="Месяц YYYY-MM"),
    by: str = typer.Option(..., "--by", help="Кто закрепляет (директор)"),
    project_id: str = typer.Option("partner", "--project"),
) -> None:
    """Месячный снапшот долей (M7.1): sha256 + git-якорь, иммутабелен."""
    result = _server_request("POST", f"/snapshots/{month}", {"actor": by})
    if result["status"] == "duplicate":
        console.print(
            f"[yellow]Снапшот {month} уже закреплен: sha256 {result['sha256'][:16]}…[/yellow]"
        )
        return
    console.print(
        f"[green]Снапшот {result['snapshot_id']} закреплен: "
        f"{result['participants']} участник(ов), sha256 {result['sha256'][:16]}…, "
        f"git-якорь {result.get('git_ref') or '—'}.[/green]"
    )


def _write_export_bundle(bundle: dict, target: pathlib.Path) -> list[pathlib.Path]:
    """Разбор пакета юристу в каталог: policy, snapshots, gates, templates."""
    import json as _json

    import yaml as _yaml

    written: list[pathlib.Path] = []

    def put(rel: str, content: str) -> None:
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(p)

    pol = bundle["policy"]
    put("policy.json", _json.dumps(pol, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    put(
        "policy.md",
        f"# Политика проекта {pol['policy_id']}\n"
        f"Версия: v{pol['policy_version']} · sha256: {pol['sha256']}\n\n"
        "```yaml\n"
        + _yaml.safe_dump(pol["params"], allow_unicode=True, sort_keys=True)
        + "```\n",
    )
    for s in bundle["snapshots"]:
        put(f"snapshots/{s['month']}.md", s["text"])
    put(
        "snapshots.json",
        _json.dumps(
            [{k: s[k] for k in ("snapshot_id", "month", "sha256", "git_ref", "created_at")}
             for s in bundle["snapshots"]],
            ensure_ascii=False, sort_keys=True, indent=2,
        ) + "\n",
    )
    gates_lines = [
        "# Гейты G-01…G-08",
        "",
        f"Модуль включен: {'да' if bundle['legal_enabled'] else 'нет'}",
        "",
        f"{'Гейт':<7} {'Приор.':<7} {'Статус':<8} Предмет",
    ]
    for g in bundle["gates"]:
        gates_lines.append(f"{g['gate_id']:<7} {g['priority']:<7} {g['status']:<8} {g['subject']}")
        if g["evidence"]:
            gates_lines.append(f"    основание: {g['evidence']}")
    put("gates.md", "\n".join(gates_lines) + "\n")
    put(
        "questions-to-lawyer.md",
        "# Вопросы юристу\n\n"
        + "\n".join(f"{i}. {q}" for i, q in enumerate(bundle["questions_to_lawyer"], 1))
        + "\n",
    )
    for rel, content in bundle["templates"].items():
        put(rel, content)
    put("MANIFEST.txt", _json.dumps(bundle["manifest"], ensure_ascii=False, indent=2) + "\n")
    return written


@app.command("export-legal")
def export_legal(
    to: pathlib.Path = typer.Option(pathlib.Path("lawyer"), "--to", help="Каталог пакета"),
    by: str = typer.Option(..., "--by", help="Кто собирает пакет"),
    project_id: str = typer.Option("partner", "--project"),
) -> None:
    """Пакет юристу (legal-module.md §7): политика, снапшоты, гейты, шаблоны."""
    bundle = _server_request("POST", "/legal/export", {"actor": by})
    files = _write_export_bundle(bundle, to)
    console.print(
        f"[green]Пакет юристу собран: {to} ({len(files)} файлов).[/green]\n"
        f"manifest bundle_sha256: {bundle['manifest']['bundle_sha256'][:16]}…\n"
        "[dim]Подпишите MANIFEST.txt и листы снапшотов; пакет отправляется юристу.[/dim]"
    )


if __name__ == "__main__":
    app()
