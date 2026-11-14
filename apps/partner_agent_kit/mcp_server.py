"""Минимальный MCP-сервер agent-kit (stdio, JSON-RPC 2.0).

Реализует подмножество MCP 2024-11-05: initialize, ping, tools/list,
tools/call. Инструменты работают только с числами учета и не получают
доступ к коду/промптам клиента (контракт agent-kit §5).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from typing import IO, Any

from . import __version__
from .client import PartnerClient, PartnerError
from .config import AgentConfig, load_config
from .session import AgentSession, Clock, utc_now

PROTOCOL_VERSION = "2024-11-05"
MIN_HEARTBEAT_GAP = timedelta(seconds=30)


class MCPToolError(Exception):
    """Ожидаемая ошибка инструмента → isError=true, без падения сервера."""


TOOLS: list[dict] = [
    {
        "name": "partner_start_session",
        "description": (
            "Начать сессию учета с привязкой к задаче и ветке. "
            "Требует подтверждения человека (confirm=true)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ref": {"type": "string", "description": "Задача/issue"},
                "branch": {"type": "string", "description": "Ветка разработки"},
                "agent_attached": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "description": "Подтверждение человека"},
            },
            "required": ["confirm"],
        },
    },
    {
        "name": "partner_heartbeat",
        "description": "Отправить один интервал активности (не чаще раза в 30 с).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "signals": {
                    "type": "object",
                    "description": (
                        "input_events, focus, tool_calls, agent_running, "
                        "files_touched, commits, diffs"
                    ),
                },
                "idle_sec": {"type": "number", "description": "Секунд без ввода"},
            },
        },
    },
    {
        "name": "partner_status",
        "description": "Текущая сессия и часы участника за месяц.",
        "inputSchema": {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "YYYY-MM"}},
        },
    },
    {
        "name": "partner_day",
        "description": "Итог дня участника (закрытый — из БД, иначе preview).",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "partner_explain",
        "description": "Раскладка начисления дня: интервалы, классы, капы, версии.",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "partner_dispute",
        "description": (
            "Открыть спор дня (окно 3 суток). Требует подтверждения человека."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "reason": {"type": "string", "minLength": 3},
                "interval_ids": {"type": "array", "items": {"type": "string"}},
                "confirm": {"type": "boolean", "description": "Подтверждение человека"},
            },
            "required": ["date", "reason", "confirm"],
        },
    },
    {
        "name": "partner_inbox",
        "description": (
            "Входящие дайджесты и уведомления участника (B-03): "
            "канал agent_chat, статус доставки и текст."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "sent", "failed"],
                    "default": "pending",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
]


def _text_result(data: Any, *, is_error: bool = False) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


class MCPServer:
    """Обработчик MCP-сообщений; handle() тестируется без stdio."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        client: PartnerClient | None = None,
        session: AgentSession | None = None,
        clock: Clock | None = None,
    ):
        self.config = config
        self.client = client or PartnerClient(config.server_url, config.api_token)
        self.session = session or AgentSession(
            config, client=self.client, clock=clock or utc_now
        )
        self.clock = clock or utc_now
        self._last_heartbeat: datetime | None = None

    # ---------- JSON-RPC ----------

    def handle(self, message: dict) -> dict | None:
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None,
                          -32600, "invalid request")
        method = message["method"]
        request_id = message.get("id")
        if request_id is None:  # notification (notifications/initialized и др.)
            return None
        if method == "initialize":
            return _ok(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "partner-agent-kit", "version": __version__},
            })
        if method == "ping":
            return _ok(request_id, {})
        if method == "tools/list":
            return _ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                return _error(request_id, -32602, f"unknown tool: {name}")
            try:
                payload = self._call_tool(name, arguments)
            except MCPToolError as err:
                return _ok(request_id, _text_result(str(err), is_error=True))
            except PartnerError as err:
                return _ok(request_id, _text_result(str(err), is_error=True))
            return _ok(request_id, _text_result(payload))
        return _error(request_id, -32601, f"method not found: {method}")

    # ---------- инструменты ----------

    def _require_confirm(self, arguments: dict, action: str) -> None:
        if arguments.get("confirm") is not True:
            raise MCPToolError(
                f"{action}: требуется подтверждение человека (confirm=true)"
            )

    def _call_tool(self, name: str, arguments: dict) -> dict:
        if name == "partner_start_session":
            self._require_confirm(arguments, "старт сессии")
            session_id = self.session.start(
                task_ref=arguments.get("task_ref"),
                branch=arguments.get("branch"),
                agent_attached=bool(arguments.get("agent_attached", True)),
            )
            return self.session.status() | {"session_id": session_id}

        if name == "partner_heartbeat":
            if not self.session.session_id:
                raise MCPToolError("сессия не начата: вызовите partner_start_session")
            now = self.clock()
            if self._last_heartbeat is not None and now - self._last_heartbeat < MIN_HEARTBEAT_GAP:
                raise MCPToolError("heartbeat не чаще одного раза в 30 секунд")
            events = self.session.heartbeat(
                arguments.get("signals") or {},
                idle_sec=arguments.get("idle_sec"),
                end_at=now,
            )
            if not events:
                raise MCPToolError("интервал еще не накоплен: heartbeat не чаще раза в 30 с")
            self._last_heartbeat = now
            return {"events": events, "pending": len(self.session.buffer)}

        if name == "partner_status":
            data = self.client.get_status(
                self.config.participant_id, arguments.get("month")
            )
            return data | {"local": self.session.status()}

        if name == "partner_day":
            date = str(arguments.get("date") or "")
            if not date:
                raise MCPToolError("date обязателен")
            return self.client.get_day(date, self.config.participant_id)

        if name == "partner_explain":
            date = str(arguments.get("date") or "")
            if not date:
                raise MCPToolError("date обязателен")
            return self.client.explain_day(date, self.config.participant_id)

        if name == "partner_dispute":
            self._require_confirm(arguments, "спор дня")
            date = str(arguments.get("date") or "")
            reason = str(arguments.get("reason") or "")
            if not date or len(reason) < 3:
                raise MCPToolError("date и reason (>=3 символов) обязательны")
            return self.client.open_dispute(
                date,
                participant_id=self.config.participant_id,
                opened_by=self.config.participant_id,
                reason=reason,
                interval_ids=list(arguments.get("interval_ids") or []),
            )

        if name == "partner_inbox":
            status = str(arguments.get("status") or "pending")
            if status not in ("pending", "sent", "failed"):
                raise MCPToolError("status: pending | sent | failed")
            try:
                limit = int(arguments.get("limit") or 20)
            except (TypeError, ValueError) as err:
                raise MCPToolError("limit должен быть числом 1..100") from err
            if not 1 <= limit <= 100:
                raise MCPToolError("limit должен быть в диапазоне 1..100")
            return self.client.list_notifications(
                recipient=self.config.participant_id,
                status=status,
                limit=limit,
            )

        raise MCPToolError(f"инструмент не реализован: {name}")

    # ---------- stdio ----------

    def serve_stdio(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        instream = stdin or sys.stdin
        outstream = stdout or sys.stdout
        for line in instream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response = _error(None, -32700, "parse error")
            else:
                response = self.handle(message)
            if response is not None:
                outstream.write(json.dumps(response, ensure_ascii=False) + "\n")
                outstream.flush()


def _ok(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> None:
    from partner_cli.console_io import configure_stdio

    configure_stdio()
    config = load_config()
    config.validate()
    MCPServer(config).serve_stdio()


if __name__ == "__main__":
    main()
