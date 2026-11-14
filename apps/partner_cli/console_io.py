"""Кроссплатформенная настройка stdout/stderr.

Windows-консоль с кодовой страницей cp1251 не умеет печатать символы ₽ и →;
без этой настройки CLI и инструменты падают с UnicodeEncodeError при
перенаправлении вывода. Ошибки кодирования заменяются на «?», кодировка
терминала не меняется (UTF-8-терминалы работают как раньше).
"""
from __future__ import annotations

import sys


def configure_stdio() -> None:
    """Не падать на символах, которых нет в кодировке консоли."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
