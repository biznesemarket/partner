"""Локальный цикл agent-kit: heartbeat каждые 30 секунд.

Присутствие человека IDE-плагин публикует в signals.json; CLI без плагина
считает работу агента автономной (человек не подтвержден). Значения
сигналов проходят белый список session.sanitize_signals.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Callable

from .config import AgentConfig
from .session import AgentSession, Clock, utc_now

SignalsProvider = Callable[[], dict]


class FileSignalsProvider:
    """Читает signals.json, который обновляет IDE-плагин.

    Ошибки/отсутствие файла → пустые сигналы: кит не выдумывает активность.
    """

    def __init__(self, path: pathlib.Path):
        self.path = path
        self._last_mtime: float | None = None
        self._last: dict = {}

    def __call__(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return {}
        if self._last_mtime == mtime:
            return dict(self._last)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        self._last_mtime = mtime
        self._last = data
        return dict(data)


def default_signals_provider(config: AgentConfig, agent_attached: bool) -> SignalsProvider:
    provider = FileSignalsProvider(config.resolved_signals_path())
    if config.resolved_signals_path().exists():
        return provider

    def fallback() -> dict:
        return {"agent_running": True} if agent_attached else {}

    return fallback


def run_daemon(
    config: AgentConfig,
    *,
    task_ref: str | None = None,
    branch: str | None = None,
    agent_attached: bool = True,
    interval: int | None = None,
    iterations: int | None = None,
    session: AgentSession | None = None,
    provider: SignalsProvider | None = None,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Запустить цикл сессии. iterations — для тестов/демо, None — до остановки."""
    clock = clock or utc_now
    sess = session or AgentSession(config, clock=clock)
    sess.start(task_ref=task_ref, branch=branch, agent_attached=agent_attached)
    provider = provider or default_signals_provider(config, agent_attached)
    delay = interval or config.heartbeat_sec
    ticks = 0
    sent = 0
    try:
        while iterations is None or ticks < iterations:
            signals = provider()
            events = sess.heartbeat(signals, end_at=clock())
            sent += len(events)
            ticks += 1
            if iterations is not None and ticks >= iterations:
                break
            sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        result = sess.stop()
    return {
        "session_id": sess.session_id,
        "ticks": ticks,
        "heartbeats": sent,
        "flush": result,
    }
