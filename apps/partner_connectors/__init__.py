"""partner-connectors: backfill фактов GitHub/Forgejo/Gitea в сервер.

Вебхуки принимает сервер (partner_server.connector_routes); этот пакет
нужен для первичной синхронизации веток/PR/issues через API платформы.
"""
from __future__ import annotations

__version__ = "1.2.0"
