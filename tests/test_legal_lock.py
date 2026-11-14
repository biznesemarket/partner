"""T-04: legal.yaml — межпроцессная блокировка и атомарная замена.

Сценарии:
- L-01  конкурентные set_gate по разным гейтам не теряют обновления
- L-02  читатель никогда не видит частично записанный YAML
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import yaml

from partner_server import legal


def test_concurrent_gate_updates_are_not_lost(tmp_path):
    """L-01: read-modify-write под _state_lock сохраняет все восемь гейтов."""
    path = tmp_path / "legal.yaml"
    gates = list(legal.GATES)

    def close_gate(gid: str) -> None:
        legal.set_gate(
            path,
            gid,
            status="done",
            evidence=f"документ {gid}: подписан 2026-11-14, юрист",
            actor="lawyer",
        )

    with ThreadPoolExecutor(max_workers=len(gates)) as pool:
        list(pool.map(close_gate, gates))

    state = legal.load_state(path)
    assert legal.unclosed_gates(state) == []
    assert all(state["gates"][gid]["status"] == "done" for gid in gates)


def test_save_state_is_atomic_for_readers(tmp_path):
    """L-02: замена через os.replace — читатель видит old или new, не torn."""
    path = tmp_path / "legal.yaml"
    legal.save_state(path, legal.default_state())
    stop = threading.Event()
    errors: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    errors.append(f"не словарь: {data!r}")
            except PermissionError:
                # Windows: os.replace и открытие файла могут кратко
                # пересечься; load_state такие окна ретраит.
                continue
            except Exception as err:  # noqa: BLE001 — фиксируем любой torn-read
                errors.append(repr(err))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(200):
            legal.save_state(path, legal.default_state())
    finally:
        stop.set()
        thread.join()

    assert not errors
