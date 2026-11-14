"""Соглашение о ветках и задачах, общее для сервера и коннекторов."""
from __future__ import annotations


def task_key(value: str | None) -> str:
    """Нормализованный ключ задачи: без '#' и в нижнем регистре."""
    if not value:
        return ""
    return value.strip().lstrip("#").lower()


def task_ref_from_branch(branch: str | None, default_branch: str = "main") -> str | None:
    """`dev/<логин>/<тема>` (development-process.md §1) → тема как task_ref."""
    if not branch or branch == default_branch:
        return None
    if branch.startswith("dev/"):
        parts = branch.split("/", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
        return None
    return branch
