"""B-03: транспорт agent_chat (push в адаптер чата) + журнал.

Сценарии:
- N-01  без PARTNER_AGENT_CHAT_URL канал остается журналом (без HTTP)
- N-02  с URL отправляется JSON и Bearer-токен адаптера
- N-03  ошибка адаптера — ошибка попытки (включается fallback)
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from partner_server import models, notify
from partner_server.config import settings


class _Resp:
    def __init__(self, status: int = 202):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _notification() -> models.Notification:
    return models.Notification(
        notification_id="ntf-b03",
        tenant_id="tenant-1",
        project_id="partner",
        kind="day_digest",
        recipient="dev1",
        status="pending",
        payload_json={"date": "2026-11-12"},
        text="Дайджест дня 2026-11-12: 6.6 ч",
    )


def test_agent_chat_without_url_is_journal_only():
    """N-01: пустой URL — прежнее поведение (только журнал сервера)."""
    old = settings.agent_chat_url
    object.__setattr__(settings, "agent_chat_url", "")
    try:
        with patch("urllib.request.urlopen") as urlopen:
            notify._send_agent_chat(None, _notification())
        urlopen.assert_not_called()
    finally:
        object.__setattr__(settings, "agent_chat_url", old)


def test_agent_chat_posts_to_adapter_with_token():
    """N-02: адаптер получает notification_id/kind/text и Bearer-токен."""
    old_url, old_token = settings.agent_chat_url, settings.agent_chat_token
    object.__setattr__(settings, "agent_chat_url", "https://chat.example/hook")
    object.__setattr__(settings, "agent_chat_token", "chat-secret")
    try:
        with patch("urllib.request.urlopen", return_value=_Resp()) as urlopen:
            notify._send_agent_chat(None, _notification())
        request = urlopen.call_args.args[0]
        assert request.full_url == "https://chat.example/hook"
        assert request.get_header("Authorization") == "Bearer chat-secret"
        body = json.loads(request.data)
        assert body["notification_id"] == "ntf-b03"
        assert body["kind"] == "day_digest"
        assert body["recipient"] == "dev1"
        assert body["text"].startswith("Дайджест дня")
        assert body["payload"] == {"date": "2026-11-12"}
        assert body["tenant_id"] == "tenant-1"
        assert body["project_id"] == "partner"
    finally:
        object.__setattr__(settings, "agent_chat_url", old_url)
        object.__setattr__(settings, "agent_chat_token", old_token)


def test_agent_chat_adapter_error_is_attempt_failure():
    """N-03: 5xx адаптера → RuntimeError, чтобы deliver ушел в fallback."""
    old = settings.agent_chat_url
    object.__setattr__(settings, "agent_chat_url", "https://chat.example/hook")
    try:
        with patch("urllib.request.urlopen", return_value=_Resp(503)):
            with pytest.raises(RuntimeError, match="agent_chat вернул 503"):
                notify._send_agent_chat(None, _notification())
    finally:
        object.__setattr__(settings, "agent_chat_url", old)
