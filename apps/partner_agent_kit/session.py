"""Жизненный цикл сессии agent-kit: start → heartbeat → pause/stop → flush.

Кит не считает деньги и не применяет политику. Он режет интервал по
локальной полуночи пояса проекта (контракт §3), классифицирует отрезок
по сигналам и отправляет подписанные heartbeat в порядке seq.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import tempfile
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from partner_cli import calc

from .buffer import OfflineBuffer
from .client import PartnerClient
from .config import AgentConfig

Clock = Callable[[], datetime]


class KitError(Exception):
    """Ошибка состояния кита (нет сессии, обратный интервал и т.п.)."""


ALLOWED_INT_SIGNALS = {
    "input_events",
    "focus",
    "scroll",
    "tool_calls",
    "files_touched",
    "commits",
    "diffs",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def sanitize_signals(signals: dict | None) -> dict:
    """Белый список сигналов: код, промпты и файлы в телеметрию не попадают."""
    if not signals:
        return {}
    clean: dict = {}
    for key in ALLOWED_INT_SIGNALS:
        if key in signals:
            try:
                clean[key] = max(0, int(signals[key] or 0))
            except (TypeError, ValueError):
                continue
    if "agent_running" in signals:
        clean["agent_running"] = bool(signals["agent_running"])
    return clean


def classify(
    signals: dict,
    *,
    agent_attached: bool,
    idle_sec: float | None = None,
    idle_timeout: int = 300,
) -> str:
    """Класс времени по сигналам присутствия человека и работы агента."""
    if idle_sec is not None and idle_sec >= idle_timeout:
        return calc.CLASS_IDLE
    human = int(signals.get("input_events", 0) or 0) > 0 or int(
        signals.get("focus", 0) or 0
    ) > 0
    agent_running = bool(signals.get("agent_running", False))
    if agent_attached and agent_running:
        return calc.CLASS_SUPERVISED if human else calc.CLASS_AUTONOMOUS
    return calc.CLASS_ACTIVE if human else calc.CLASS_IDLE


def split_at_midnight(
    start: datetime, end: datetime, tz_name: str
) -> list[tuple[datetime, datetime]]:
    """Разрезать [start, end) по локальным полуночам пояса проекта (UTC-пары)."""
    tz = ZoneInfo(tz_name)
    cur = as_utc(start).astimezone(tz)
    stop = as_utc(end).astimezone(tz)
    segments: list[tuple[datetime, datetime]] = []
    while cur < stop:
        next_day = cur.date() + timedelta(days=1)
        next_midnight = datetime.combine(next_day, time.min, tzinfo=tz)
        seg_end = min(stop, next_midnight)
        segments.append((cur.astimezone(timezone.utc), seg_end.astimezone(timezone.utc)))
        cur = seg_end
    return segments


def _new_id(prefix: str, at: datetime) -> str:
    return f"{prefix}-{as_utc(at).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


class AgentSession:
    """Одна сессия учета; состояние переживает перезапуск процесса."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        client: PartnerClient | None = None,
        buffer: OfflineBuffer | None = None,
        clock: Clock | None = None,
    ):
        config.validate()
        self.config = config
        self.client = client or PartnerClient(config.server_url, config.api_token)
        self.buffer = buffer or OfflineBuffer(config.resolved_buffer_path())
        self.clock = clock or utc_now
        self.state_path = pathlib.Path(str(self.buffer.path) + ".state")

        self.session_id: str | None = None
        self.seq = 0
        self.started_at: datetime | None = None
        self.last_end: datetime | None = None
        self.task_ref: str | None = None
        self.branch: str | None = None
        self.agent_attached = True
        self.paused = False
        self.stopped = False
        self._restore()

    # ---------- состояние ----------

    def _restore(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not state.get("session_id") or state.get("stopped"):
            return
        self.session_id = state["session_id"]
        self.seq = int(state.get("seq", 0))
        self.started_at = parse_iso(state["started_at_utc"])
        self.last_end = parse_iso(state["last_end_utc"])
        self.task_ref = state.get("task_ref")
        self.branch = state.get("branch")
        self.agent_attached = bool(state.get("agent_attached", True))
        self.paused = bool(state.get("paused", False))

    def _state(self) -> dict:
        return {
            "session_id": self.session_id,
            "seq": self.seq,
            "started_at_utc": to_iso(self.started_at) if self.started_at else None,
            "last_end_utc": to_iso(self.last_end) if self.last_end else None,
            "task_ref": self.task_ref,
            "branch": self.branch,
            "agent_attached": self.agent_attached,
            "paused": self.paused,
            "stopped": self.stopped,
        }

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._state(), ensure_ascii=False, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self.state_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _clear_state(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    # ---------- жизненный цикл ----------

    def start(
        self,
        *,
        task_ref: str | None = None,
        branch: str | None = None,
        agent_attached: bool = True,
        started_at: datetime | None = None,
    ) -> str:
        if self.session_id:
            self.flush()
            return self.session_id
        self.task_ref = task_ref if task_ref is not None else self.config.task_ref
        self.branch = branch if branch is not None else self.config.branch
        self.agent_attached = agent_attached
        self.started_at = as_utc(started_at or self.clock())
        self.last_end = self.started_at
        self.session_id = _new_id("ses", self.started_at)
        payload = {
            "session_id": self.session_id,
            "participant_id": self.config.participant_id,
            "project_id": self.config.project_id,
            "task_ref": self.task_ref,
            "branch": self.branch,
            "agent_attached": self.agent_attached,
            "started_at_utc": to_iso(self.started_at),
        }
        self.buffer.append(
            {"kind": "session", "session_id": self.session_id, "payload": payload}
        )
        self._save_state()
        self.flush()
        return self.session_id

    def heartbeat(
        self,
        signals: dict | None = None,
        *,
        idle_sec: float | None = None,
        end_at: datetime | None = None,
    ) -> list[dict]:
        if not self.session_id or self.started_at is None or self.last_end is None:
            raise KitError("сессия не начата: вызовите start()")
        if self.stopped:
            raise KitError("сессия уже завершена")
        end = as_utc(end_at or self.clock())
        if end < self.last_end:
            raise KitError("end_at раньше конца предыдущего интервала")
        if end == self.last_end:
            return []

        clean = sanitize_signals(signals)
        cls = (
            calc.CLASS_IDLE
            if self.paused
            else classify(
                clean,
                agent_attached=self.agent_attached,
                idle_sec=idle_sec,
                idle_timeout=self.config.idle_timeout_sec,
            )
        )
        queued_at = to_iso(self.clock())
        events: list[dict] = []
        for seg_start, seg_end in split_at_midnight(self.last_end, end, self.config.timezone):
            event = {
                "event_id": _new_id("evt", seg_start),
                "session_id": self.session_id,
                "seq": self.seq,
                "cls": cls,
                "start_utc": to_iso(seg_start),
                "end_utc": to_iso(seg_end),
                "signals": clean,
                "received_at_utc": queued_at,
            }
            self.seq += 1
            self.buffer.append(
                {"kind": "heartbeat", "event_id": event["event_id"], "payload": event}
            )
            events.append(event)
        self.last_end = end
        self._save_state()
        self.flush()
        return events

    def pause(self) -> None:
        if not self.session_id:
            raise KitError("сессия не начата")
        self.paused = True
        self._save_state()

    def resume(self) -> None:
        if not self.session_id:
            raise KitError("сессия не начата")
        self.paused = False
        self._save_state()

    def stop(self, end_at: datetime | None = None) -> dict:
        if not self.session_id:
            raise KitError("сессия не начата")
        if not self.stopped:
            end = as_utc(end_at or self.clock())
            if self.last_end is not None and end > self.last_end:
                self.heartbeat(
                    {"agent_running": False},
                    idle_sec=self.config.idle_timeout_sec,
                    end_at=end,
                )
            self.stopped = True
            self._save_state()
        result = self.flush()
        self._clear_state()
        return result

    # ---------- доставка ----------

    def _deliver(self, item: dict) -> object:
        if item.get("kind") == "session":
            payload = item["payload"]
            return self.client.create_session(
                session_id=payload["session_id"],
                participant_id=payload["participant_id"],
                project_id=payload["project_id"],
                task_ref=payload.get("task_ref"),
                branch=payload.get("branch"),
                agent_attached=bool(payload.get("agent_attached", True)),
                started_at_utc=payload["started_at_utc"],
            )
        payload = dict(item["payload"])
        # received_at — фактическое время отправки: офлайн-буфер не маскирует
        # опоздание, сервер применит late_rejected по grace_hours (T-09).
        payload["received_at_utc"] = to_iso(self.clock())
        return self.client.send_heartbeat(
            payload,
            device_id=self.config.device_id,
            device_secret=self.config.device_secret,
        )

    def flush(self) -> dict:
        return self.buffer.flush(self._deliver)

    # ---------- наблюдаемость ----------

    def status(self) -> dict:
        return {
            "session_id": self.session_id,
            "participant_id": self.config.participant_id,
            "project_id": self.config.project_id,
            "task_ref": self.task_ref,
            "branch": self.branch,
            "started_at_utc": to_iso(self.started_at) if self.started_at else None,
            "last_end_utc": to_iso(self.last_end) if self.last_end else None,
            "seq": self.seq,
            "agent_attached": self.agent_attached,
            "paused": self.paused,
            "stopped": self.stopped,
            "pending_events": len(self.buffer),
            "device_id": self.config.device_id,
        }
