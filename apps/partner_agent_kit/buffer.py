"""Офлайн-буфер агент-кита: события на диск, отправка в порядке seq.

Формат — JSON Lines; каждая строка — конверт {"kind": "session"|"heartbeat",
...}. Запись атомарная (temp + os.replace), права 0600. Идемпотентность
на сервере обеспечивает event_id, поэтому повторная отправка безопасна.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Callable

from .client import PartnerHTTPError, PartnerOfflineError

Deliver = Callable[[dict], object]

# Статусы, после которых событие точно не станет валидным: 422 (duplicate_seq,
# bad interval), 404 (сессии нет), 409. 401/403/5xx — конфигурация/сеть,
# такие события остаются в буфере до исправления.
PERMANENT_HTTP = {400, 404, 409, 422}


def _event_key(item: dict) -> str:
    if item.get("kind") == "session":
        return "session:" + str(item.get("session_id") or item.get("payload", {}).get("session_id"))
    return "heartbeat:" + str(item.get("event_id") or item.get("payload", {}).get("event_id"))


class OfflineBuffer:
    """Персистентная очередь одного агента (один процесс-владелец)."""

    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.corrupt_lines = 0
        self._items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        items: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                self.corrupt_lines += 1
                continue
            if isinstance(item, dict) and item.get("kind") in ("session", "heartbeat"):
                items.append(item)
            else:
                self.corrupt_lines += 1
        return items

    def _write(self) -> None:
        if not self._items:
            if self.path.exists():
                self.path.unlink()
            return
        fd, tmp_name = tempfile.mkstemp(prefix=".buffer-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for item in self._items:
                    fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def append(self, item: dict) -> bool:
        """Добавить конверт; повтор ключа в очереди не дублируется."""
        key = _event_key(item)
        if any(_event_key(existing) == key for existing in self._items):
            return False
        self._items.append(item)
        self._write()
        return True

    def __len__(self) -> int:
        return len(self._items)

    def pending(self) -> list[dict]:
        return list(self._items)

    def flush(self, deliver: Deliver) -> dict:
        """Отправить очередь по порядку; вернуть счетчики для CLI/диагностики."""
        sent = 0
        failed: list[dict] = []
        remaining: list[dict] = []
        index = 0
        while index < len(self._items):
            item = self._items[index]
            try:
                deliver(item)
            except PartnerOfflineError:
                remaining = self._items[index:]
                break
            except PartnerHTTPError as err:
                if err.status in (401, 403) or err.status >= 500:
                    remaining = self._items[index:]
                    break
                if err.status in PERMANENT_HTTP:
                    failed.append({"key": _event_key(item), "code": err.code,
                                   "status": err.status, "message": err.message})
                    index += 1
                    continue
                remaining = self._items[index:]
                break
            sent += 1
            index += 1
        self._items = remaining
        self._write()
        return {"sent": sent, "failed": failed, "pending": len(self._items)}
