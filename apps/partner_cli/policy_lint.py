"""Валидация policy.yaml (POLICY-SCHEMA 1.0) — модуль пакета partner_cli.

Используется из репозитория продукта (tools/policy_lint.py) и из
репозиториев клиентов (repo-pack/workflows/partner-policy-check.yml):
python -m partner_cli.policy_lint policy.yaml

Проверки: JSON Schema + кросс-полевые (X-01..X-09) + версионные (V-01..V-08).
Запуск: python -m partner_cli.policy_lint path/to/policy.yaml [--as-of YYYY-MM-DD]
"""
import sys
import json
import pathlib
import argparse
from datetime import datetime, timedelta, timezone
from importlib import resources

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError:
    print("ERROR: pip install pyyaml jsonschema")
    sys.exit(1)

MSK = timezone(timedelta(hours=3))


def _load_schema() -> dict:
    candidates = []
    root = pathlib.Path.cwd()
    for base in (root, *root.parents):
        candidates.append(base / "docs" / "02-policy" / "policy.schema.json")
    for c in candidates:
        if c.exists():
            return json.loads(c.read_text(encoding="utf-8"))
    return json.loads(
        resources.files("partner_cli").joinpath("policy.schema.json").read_text(encoding="utf-8")
    )


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def lint(path: str, as_of_arg: str | None = None) -> list[str]:
    pol = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    errors: list[str] = []
    for e in sorted(Draft202012Validator(_load_schema()).iter_errors(pol), key=lambda x: list(x.path)):
        loc = "/".join(map(str, e.path)) or "(root)"
        errors.append(f"[schema] {loc}: {e.message}")

    eco = pol.get("economics", {})
    tm = pol.get("time", {})
    vest = pol.get("vesting", {})
    legal = pol.get("legal", {})

    if eco.get("pool_pct", 0) + eco.get("founder_floor_pct", 0) > 100:
        errors.append("[X-01] pool_pct + founder_floor_pct > 100")
    if tm.get("autonomous_paid_cap_hours", 0) > tm.get("daily_cap_hours", 24):
        errors.append("[X-02] автономный кап больше дневного")
    if tm.get("presence_window_sec", 0) <= tm.get("active_window_sec", 0):
        errors.append("[X-03] presence_window должен быть больше active_window")
    if tm.get("idle_timeout_sec", 0) < tm.get("active_window_sec", 0):
        errors.append("[X-04] idle_timeout не может быть меньше active_window")
    if vest.get("cliff_months", 0) > vest.get("months", 48):
        errors.append("[X-05] cliff_months > months")
    mode, rule = tm.get("mode"), vest.get("active_month_rule")
    if mode == "time_based" and rule not in (None, "paid_hours"):
        errors.append("[X-06] time_based требует active_month_rule=paid_hours")
    if mode == "fixed_price" and rule not in (None, "amount"):
        errors.append("[X-07] fixed_price требует active_month_rule=amount")
    if legal.get("enabled") and not (legal.get("jurisdiction") and legal.get("entity_name")):
        errors.append("[X-08] legal.enabled требует jurisdiction и entity_name")
    if eco.get("pool_info_pct", 80) >= eco.get("pool_warn_pct", 90):
        errors.append("[X-09] pool_info_pct должен быть меньше pool_warn_pct")

    as_of = (parse_dt(as_of_arg) if as_of_arg else datetime.now(MSK)) or datetime.now(MSK)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=MSK)
    day_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)

    for kind, current in (("rate", eco.get("rate_rub_per_hour")),
                          ("valuation", eco.get("valuation_rub"))):
        entries = pol.get("versions", {}).get(kind, [])
        if not entries:
            errors.append(f"[V-01] versions.{kind} пуст")
            continue
        prev, active = None, None
        for i, en in enumerate(entries):
            dt = parse_dt(en.get("effective_from"))
            if not dt:
                errors.append(f"[V-06] versions.{kind}[{i}]: effective_from не разобран")
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MSK)
            if prev and dt <= prev:
                errors.append(f"[V-02] versions.{kind}[{i}]: effective_from не возрастает")
            prev = dt
            if en.get("value", 0) <= 0:
                errors.append(f"[V-03] versions.{kind}[{i}]: value <= 0")
            if len(str(en.get("reason", ""))) < 5:
                errors.append(f"[V-06] versions.{kind}[{i}]: reason короче 5 символов")
            if not en.get("approved_by"):
                errors.append(f"[V-06] versions.{kind}[{i}]: нет approved_by")
            if kind == "valuation" and not en.get("adr_ref"):
                errors.append(f"[V-05] versions.valuation[{i}]: нет adr_ref")
            if i > 0 and dt < day_start:
                errors.append(f"[V-04] versions.{kind}[{i}]: дата в прошлом — заднее число запрещено")
            if dt <= as_of:
                active = en
        if active and current is not None and abs(float(active["value"]) - float(current)) > 0.001:
            code = "V-07" if kind == "rate" else "V-08"
            errors.append(f"[{code}] economics.{kind} ({current}) != активная версия ({active['value']})")
    return errors


def main():
    from partner_cli.console_io import configure_stdio

    configure_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("policy")
    ap.add_argument("--as-of", default=None)
    a = ap.parse_args()
    errors = lint(a.policy, a.as_of)
    if errors:
        print("\n".join("ERROR " + e for e in errors))
        sys.exit(1)
    pol = yaml.safe_load(pathlib.Path(a.policy).read_text(encoding="utf-8"))
    print(f"OK: политика валидна · tenant={pol['meta']['tenant_id']} "
          f"project={pol['meta']['project_id']} · режим={pol['time'].get('mode')} · "
          f"ставка={pol['economics'].get('rate_rub_per_hour')} ₽/ч · "
          f"оценка={pol['economics'].get('valuation_rub')} ₽")


if __name__ == "__main__":
    main()
