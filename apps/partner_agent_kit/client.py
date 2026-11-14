"""HTTP-клиент сервера «Партнер» для agent-kit.

Зависимостей нет: urllib из стандартной библиотеки. Транспорт можно
подменить (transport=callable) — так тесты работают против FastAPI
TestClient без реального сокета.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from partner_cli.signing import sign_heartbeat


class PartnerError(Exception):
    """Базовая ошибка кита."""


class PartnerOfflineError(PartnerError):
    """Сервер недоступен: событие остается в офлайн-буфере."""


class PartnerHTTPError(PartnerError):
    """HTTP-ответ 4xx/5xx с кодом домена."""

    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"HTTP {status} {code}: {message}")


Transport = Callable[
    [str, str, bytes | None, dict[str, str], float], tuple[int, dict]
]


def _decode_detail(data: dict) -> tuple[str, str]:
    detail = data.get("detail")
    if isinstance(detail, dict):
        return (
            str(detail.get("code") or data.get("code") or "http_error"),
            str(detail.get("detail") or data.get("message") or detail),
        )
    if isinstance(detail, str):
        return str(data.get("code") or "http_error"), detail
    return (
        str(data.get("code") or "http_error"),
        str(data.get("message") or data),
    )


def urllib_transport(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict]:
    """Боевой транспорт: urllib; сетевые ошибки → PartnerOfflineError."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:  # подкласс URLError — первым
        raw = err.read()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"detail": raw.decode("utf-8", "replace")}
        return err.code, data
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise PartnerOfflineError(str(err)) from err


class PartnerClient:
    """Тонкий клиент API: сессии, heartbeat, чтение дня, споры, устройства."""

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        *,
        timeout: float = 15.0,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.transport = transport or urllib_transport

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        url = self.base_url + "/v1" + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if extra_headers:
            headers.update(extra_headers)
        status, payload = self.transport(method, url, data, headers, self.timeout)
        if status >= 400:
            code, message = _decode_detail(payload)
            raise PartnerHTTPError(status, code, message)
        return payload

    # ---------- устройства ----------

    def register_device(self, participant_id: str, label: str = "") -> dict:
        return self._request(
            "POST", "/devices", {"participant_id": participant_id, "label": label}
        )

    def revoke_device(self, device_id: str) -> dict:
        return self._request("POST", f"/devices/{device_id}/revoke")

    # ---------- время ----------

    def create_session(
        self,
        *,
        session_id: str,
        participant_id: str,
        project_id: str,
        task_ref: str | None,
        branch: str | None,
        agent_attached: bool,
        started_at_utc: str,
    ) -> dict:
        return self._request(
            "POST",
            "/sessions",
            {
                "session_id": session_id,
                "participant_id": participant_id,
                "project_id": project_id,
                "task_ref": task_ref,
                "branch": branch,
                "agent_attached": agent_attached,
                "started_at_utc": started_at_utc,
            },
        )

    def send_heartbeat(
        self,
        event: dict,
        *,
        device_id: str | None = None,
        device_secret: str | None = None,
    ) -> dict:
        headers: dict[str, str] = {}
        if device_id and device_secret:
            headers["X-Partner-Device"] = device_id
            headers["X-Partner-Sign"] = sign_heartbeat(
                device_secret,
                event["event_id"],
                event["session_id"],
                int(event["seq"]),
                event["start_utc"],
                event["end_utc"],
            )
        return self._request("POST", "/heartbeats", event, headers)

    # ---------- чтение ----------

    def get_status(self, participant_id: str, month: str | None = None) -> dict:
        path = f"/status/{participant_id}"
        if month:
            path += "?" + urllib.parse.urlencode({"month": month})
        return self._request("GET", path)

    def get_day(self, date: str, participant_id: str) -> dict:
        query = urllib.parse.urlencode({"participant_id": participant_id})
        return self._request("GET", f"/days/{date}?{query}")

    def explain_day(self, date: str, participant_id: str) -> dict:
        query = urllib.parse.urlencode({"participant_id": participant_id})
        return self._request("GET", f"/days/{date}/explain?{query}")

    def list_notifications(
        self,
        *,
        recipient: str | None = None,
        status: str = "pending",
        limit: int = 50,
    ) -> dict:
        """B-03: входящие уведомления/дайджесты для чата агента."""
        params = {"status": status, "limit": limit}
        if recipient:
            params["recipient"] = recipient
        return self._request(
            "GET", "/notifications?" + urllib.parse.urlencode(params)
        )

    def open_dispute(
        self,
        date: str,
        *,
        participant_id: str,
        opened_by: str,
        reason: str,
        interval_ids: list[str] | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"/days/{date}/disputes",
            {
                "participant_id": participant_id,
                "opened_by": opened_by,
                "reason": reason,
                "interval_ids": interval_ids or [],
            },
        )
