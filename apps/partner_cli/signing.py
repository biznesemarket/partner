"""Каноническая строка и HMAC-подпись heartbeat (agent-kit ↔ сервер).

Контракт docs/03-architecture/agent-kit.md §4: подпись устройства —
HMAC-SHA256(secret, "event_id|session_id|seq|start_utc|end_utc").
Одна реализация для клиента и сервера исключает расхождение формата.
"""
from __future__ import annotations

import hashlib
import hmac


def canonical_heartbeat_line(
    event_id: str,
    session_id: str,
    seq: int,
    start_utc: str,
    end_utc: str,
) -> str:
    """Строка подписи ровно в порядке контракта agent-kit §4."""
    return f"{event_id}|{session_id}|{seq}|{start_utc}|{end_utc}"


def sign_heartbeat(
    secret: str,
    event_id: str,
    session_id: str,
    seq: int,
    start_utc: str,
    end_utc: str,
) -> str:
    """hex HMAC-SHA256 подписи события; secret — hex-строка устройства."""
    line = canonical_heartbeat_line(event_id, session_id, seq, start_utc, end_utc)
    return hmac.new(
        secret.encode("utf-8"), line.encode("utf-8"), hashlib.sha256
    ).hexdigest()
