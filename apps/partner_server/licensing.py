"""M9.1: лицензирование закрытой подписки (ADR-0018).

Формат ключа (env PARTNER_LICENSE_KEY):

    PARTNER.<tenant_id>.<edition>.<YYYY-MM-DD>.<signature-64-hex>

signature = HMAC-SHA256(PARTNER_LICENSE_SECRET, f"{tenant_id}|{edition}|{expires}")
Секрет поставки выдается клиенту вне канала ключа (licensing-pricing.md §механика);
секрет — per-tenant, поэтому компрометация одного секрета не подделывает
чужие ключи; продление своего срока — нарушение договора, вскрывается
сверкой с реестром выдачи вендора.

Состояния (state):

- demo      — ключ не задан и PARTNER_LICENSE_REQUIRED=0 (разработка/демо),
              все функции работают;
- invalid   — ключ задан, но подпись/формат не сходятся; закрытие дня
              блокируется (чтение и экспорт — нет: «данные клиента —
              у клиента», vision), инцидент в аудит при попытке;
- active    — срок не истек;
- grace     — срок истек, но PARTNER_LICENSE_GRACE_DAYS не прошли:
              работает полноценно, /v1/license и лог предупреждают;
- expired   — grace прошел: новые закрытия дня блокируются
              (DomainError license_expired → 402), чтение/экспорт/снятие
              системы разрешены.

Модуль не хранит состояния — все вычисляется из env на вызов; смена ключа
действует сразу после перезапуска процесса (uvicorn reload / compose up).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import date

from .config import Settings

log = logging.getLogger("partner.license")

STATES = ("demo", "invalid", "active", "grace", "expired")


class LicenseError(Exception):
    """Формат/подпись ключа не сходятся (state=invalid)."""


@dataclass(frozen=True)
class LicenseState:
    state: str
    tenant_id: str | None = None
    edition: str | None = None
    expires_at: str | None = None
    days_left: int | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "tenant_id": self.tenant_id,
            "edition": self.edition,
            "expires_at": self.expires_at,
            "days_left": self.days_left,
            "detail": self.detail,
        }


def signature(secret: str, tenant_id: str, edition: str, expires: str) -> str:
    """Подпись ключа (вендорская выдача: tools/license_issue.py)."""
    msg = f"{tenant_id}|{edition}|{expires}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def issue_key(secret: str, tenant_id: str, edition: str, expires: str) -> str:
    """Выдать ключ по данным (используется вендором и тестами)."""
    return f"PARTNER.{tenant_id}.{edition}.{expires}.{signature(secret, tenant_id, edition, expires)}"


def _parse(key: str, secret: str) -> dict:
    parts = key.split(".")
    if (
        len(parts) != 5
        or parts[0] != "PARTNER"
        or not parts[1]
        or not parts[2]
        or len(parts[4]) != 64
    ):
        raise LicenseError("формат ключа: PARTNER.<tenant>.<edition>.<дата>.<подпись64>")
    tenant_id, edition, expires, sig = parts[1], parts[2], parts[3], parts[4]
    expected = signature(secret, tenant_id, edition, expires)
    if not hmac.compare_digest(sig, expected):
        raise LicenseError("подпись ключа не сходится с PARTNER_LICENSE_SECRET")
    return {"tenant_id": tenant_id, "edition": edition, "expires": expires}


def current_state(settings: Settings, today: date | None = None) -> LicenseState:
    """Вычислить состояние лицензии (чистая функция от env + даты)."""
    today = today or date.today()
    if not settings.license_key:
        if settings.license_required:
            return LicenseState(
                state="invalid",
                detail="PARTNER_LICENSE_REQUIRED=1, но ключ не задан",
            )
        return LicenseState(state="demo", detail="ключ не задан — demo-режим")
    try:
        parsed = _parse(settings.license_key, settings.license_secret)
    except LicenseError as err:
        return LicenseState(state="invalid", detail=str(err))
    try:
        expires = date.fromisoformat(parsed["expires"])
    except ValueError:
        return LicenseState(state="invalid", detail="дата ключа не YYYY-MM-DD")
    days_left = (expires - today).days
    if days_left >= 0:
        state = "active"
    elif days_left > -settings.license_grace_days:
        state = "grace"
    else:
        state = "expired"
    return LicenseState(
        state=state,
        tenant_id=parsed["tenant_id"],
        edition=parsed["edition"],
        expires_at=parsed["expires"],
        days_left=days_left,
        detail=f"grace {settings.license_grace_days} дн.",
    )


class LicenseBlocked(Exception):
    """Закрытие дня запрещено состоянием лицензии (state invalid/expired)."""

    def __init__(self, state: LicenseState):
        self.state = state
        super().__init__(f"license_{state.state}")


def ensure_can_close(settings: Settings, today: date | None = None) -> LicenseState:
    """Проверка перед закрытием дня: demo/active/grace — разрешено."""
    st = current_state(settings, today)
    if st.state in ("demo", "active", "grace"):
        if st.state == "grace":
            log.warning("лицензия %s истекла %s дн. назад — грейс-период", st.tenant_id, -st.days_left)
        return st
    raise LicenseBlocked(st)


def metrics_gauge(st: LicenseState) -> dict[str, float]:
    """Метрика для /v1/metrics: дней до конца срока (gauge)."""
    if st.state in ("active", "grace") and st.days_left is not None:
        return {"partner_license_days_left": st.days_left}
    return {"partner_license_days_left": 0}
