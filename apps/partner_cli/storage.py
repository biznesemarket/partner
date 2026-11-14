"""Хранилище прототипа: SQLite (режим демо/оценки). Целевая БД — PostgreSQL (I4)."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from decimal import Decimal

from . import calc

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")


def connect(db_path: str | pathlib.Path) -> sqlite3.Connection:
    path = pathlib.Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def save_day_record(
    conn: sqlite3.Connection,
    participant_id: str,
    date_iso: str,
    tz: str,
    result: calc.DayResult,
    rate_version: int = 1,
    valuation_version: int = 1,
    policy_version: int = 1,
    cash_share_pct: float = 0.0,
) -> str:
    paid_seconds = int(result.paid_hours * calc.H)
    # Аудит-фикс F2b: cash/equity из политики (R8), а не захардкоженный 0.0.
    cash_cents, equity_cents = calc.cash_split(result.amount_cents, cash_share_pct)
    payload = json.dumps(
        {
            "participant": participant_id,
            "date": date_iso,
            "paid_seconds": paid_seconds,
            "amount_cents": result.amount_cents,
            "flags": result.flags,
            "breakdown": result.breakdown,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    day_id = f"{participant_id}|{date_iso}"
    # Аудит-фикс F6 (P1): INSERT OR REPLACE молча перезаписывал ЗАКРЫТУЮ
    # запись другими числами (инвариант «закрытые дни неизменны»,
    # schema.sql §adjustments). Разрешен только идемпотентный повтор
    # с тем же sha256_input.
    existing = conn.execute(
        "SELECT sha256_input FROM day_records WHERE day_id=?", (day_id,)
    ).fetchone()
    if existing is not None and existing["sha256_input"] != sha:
        raise ValueError(
            f"день {day_id} уже закрыт с другим содержимым "
            f"(sha {existing['sha256_input'][:12]}…); правки закрытого дня — только adjustments"
        )
    conn.execute(
        """INSERT OR REPLACE INTO day_records
           (day_id, participant_id, date, tz, paid_seconds, amount_cents,
            cash_cents, equity_cents, share_bp, rate_version, valuation_version,
            policy_version, sha256_input, closed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            day_id,
            participant_id,
            date_iso,
            tz,
            paid_seconds,
            result.amount_cents,
            cash_cents,
            equity_cents,
            str(result.share_bp),
            rate_version,
            valuation_version,
            policy_version,
            sha,
        ),
    )
    for code in result.flags:
        conn.execute(
            "INSERT OR REPLACE INTO flags (day_id, code, detail) VALUES (?,?,?)",
            (day_id, code, result.breakdown.get("note")),
        )
    conn.commit()
    return sha


def month_totals(conn: sqlite3.Connection, month: str) -> dict:
    rows = conn.execute(
        "SELECT participant_id, SUM(paid_seconds) ps, SUM(amount_cents) amt "
        "FROM day_records WHERE substr(date,1,7)=? GROUP BY participant_id",
        (month,),
    ).fetchall()
    total_cents = sum(r["amt"] or 0 for r in rows)
    return {
        "participants": [
            {
                "participant_id": r["participant_id"],
                "paid_hours": str(Decimal(r["ps"] or 0) / 3600),
                "amount_rub": (r["amt"] or 0) / 100,
            }
            for r in rows
        ],
        "month_total_rub": total_cents / 100,
    }
