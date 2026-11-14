"""Конфигурация сервера через переменные окружения (07-ops/installation.md §переменные).

Все секреты — только через env; дефолты пригодны для локального запуска и тестов.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_url: str = "sqlite:///./data/partner_server.db"
    # Пустой токен = demo-режим без авторизации (только для local); в prod задавать обязательно
    api_token: str = ""
    # Планировщик закрытия дня 00:00 (ADR-0017): 1 — включен при старте uvicorn
    scheduler_enabled: bool = False
    # Грейс приема опоздавших событий в часах (time-accounting.md §7, T-08/T-09)
    grace_hours: float = 24.0
    # Часовой пояс закрытия дня по умолчанию (переопределяется политикой проекта)
    close_tz: str = "Europe/Moscow"
    # M6.3 каналы доставки дайджестов. Порядок: агент-чат (основной),
    # email (fallback), webhook (мессенджеры). Канал включается, только
    # если задан его адрес; agent_chat встроен (доставка в журнал/CLI).
    notify_channels: tuple[str, ...] = ("agent_chat", "email", "webhook")
    webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = "partner@example.local"
    smtp_to: str = ""
    # Число попыток доставки до перевода в failed
    notify_max_attempts: int = 3
    # M6.4 окно спора дня в календарных днях (time-accounting.md §спор)
    dispute_window_days: int = 3
    # M7 юридический модуль: файл состояния (legal.enabled + гейты G-01…G-08).
    # Редактируется юристом; в git клиента НЕ попадает (legal-module.md §6).
    legal_file: str = "legal.yaml"
    # M9.1 лицензирование закрытой подписки (ADR-0018). Ключ и секрет выдаются
    # при оформлении подписки; REQUIRED=1 для боевых установок (поставляется
    # в deploy/.env с REQUIRED=1). Пустой ключ при REQUIRED=0 — demo-режим.
    license_key: str = ""
    license_secret: str = ""
    license_required: bool = False
    # Грейс-период после истечения срока ключа, дней (работа с предупреждением)
    license_grace_days: int = 14
    # M9 (threat-model §4): требовать подпись устройства на каждом heartbeat.
    # 0 — подпись проверяется, если передана; 1 — неподписанные батчи отклоняются.
    require_device_sign: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_url=os.getenv("PARTNER_DB_URL", cls.db_url),
            api_token=os.getenv("PARTNER_API_TOKEN", ""),
            scheduler_enabled=os.getenv("PARTNER_SCHEDULER", "0") == "1",
            grace_hours=float(os.getenv("PARTNER_GRACE_HOURS", "24")),
            close_tz=os.getenv("PARTNER_CLOSE_TZ", cls.close_tz),
            webhook_url=os.getenv("PARTNER_WEBHOOK_URL", ""),
            smtp_host=os.getenv("PARTNER_SMTP_HOST", ""),
            smtp_port=int(os.getenv("PARTNER_SMTP_PORT", "587")),
            smtp_from=os.getenv("PARTNER_SMTP_FROM", cls.smtp_from),
            smtp_to=os.getenv("PARTNER_SMTP_TO", ""),
            notify_max_attempts=int(os.getenv("PARTNER_NOTIFY_ATTEMPTS", "3")),
            dispute_window_days=int(os.getenv("PARTNER_DISPUTE_DAYS", "3")),
            legal_file=os.getenv("PARTNER_LEGAL_FILE", cls.legal_file),
            license_key=os.getenv("PARTNER_LICENSE_KEY", ""),
            license_secret=os.getenv("PARTNER_LICENSE_SECRET", ""),
            license_required=os.getenv("PARTNER_LICENSE_REQUIRED", "0") == "1",
            license_grace_days=int(os.getenv("PARTNER_LICENSE_GRACE_DAYS", "14")),
            require_device_sign=os.getenv("PARTNER_REQUIRE_DEVICE_SIGN", "0") == "1",
        )


settings = Settings.from_env()
