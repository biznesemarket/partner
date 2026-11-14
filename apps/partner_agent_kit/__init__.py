"""partner-agent-kit: клиент учета времени для IDE/CLI и MCP.

Контракт — docs/03-architecture/agent-kit.md. Кит только собирает факты
времени (сессии, heartbeat) и передает их серверу; расчет — серверный
golden-калькулятор partner_cli.calc.
"""
from __future__ import annotations

__version__ = "1.2.0"
