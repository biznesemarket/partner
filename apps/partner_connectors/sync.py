"""Отправка нормализованных фактов в сервер «Партнер»."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import ForgeConnector


class SyncError(Exception):
    pass


def sync(
    connector: ForgeConnector,
    *,
    server_url: str,
    project_id: str,
    api_token: str = "",
    timeout: float = 30.0,
) -> dict:
    """Собрать ветки/PR/issues через API и отправить одним идемпотентным пакетом."""
    items = connector.import_items()
    body = json.dumps(
        {
            "project_id": project_id,
            "repo": connector.repo,
            "forge": connector.forge,
            "source": "api",
            "items": items,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        server_url.rstrip("/") + f"/v1/connectors/{connector.forge}/import",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            **({"Authorization": f"Bearer {api_token}"} if api_token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        raise SyncError(f"HTTP {err.code}: {detail}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise SyncError(str(err)) from err
