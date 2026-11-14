"""Общая часть коннекторов: HTTP к API платформы и нормализация фактов."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from partner_cli.branching import task_ref_from_branch

ApiTransport = Callable[[str, dict[str, str], float], Any]


class ConnectorAPIError(Exception):
    pass


def urllib_api_transport(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as err:
        raise ConnectorAPIError(str(err)) from err


class ForgeConnector:
    """Базовый клиент API; наследники задают пути и разбор ответов."""

    forge = "base"

    def __init__(
        self,
        repo: str,
        *,
        token: str = "",
        base_url: str = "",
        timeout: float = 20.0,
        transport: ApiTransport | None = None,
    ):
        self.repo = repo
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or urllib_api_transport

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "partner-connector/1.1"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return self.transport(url, headers, self.timeout)

    def list_branches(self) -> list[dict]:
        raise NotImplementedError

    def list_pull_requests(self) -> list[dict]:
        raise NotImplementedError

    def list_issues(self) -> list[dict]:
        raise NotImplementedError

    def import_items(self) -> list[dict]:
        """Нормализованные факты для POST /v1/connectors/{forge}/import."""
        items: list[dict] = []
        for branch in self.list_branches():
            name = branch.get("name")
            if not name:
                continue
            commit = branch.get("commit") or {}
            items.append(
                {
                    "kind": "branch",
                    "ref": name,
                    "branch": name,
                    "task_ref": task_ref_from_branch(name),
                    "actor": None,
                    "occurred_at": None,
                    "payload": {
                        "commit": commit.get("sha") or commit.get("id"),
                        "protected": bool(branch.get("protected", False)),
                    },
                }
            )
        for pr in self.list_pull_requests():
            head = (pr.get("head") or {}).get("ref")
            number = pr.get("number")
            user = pr.get("user") or {}
            items.append(
                {
                    "kind": "pull_request",
                    "ref": f"#{number}" if number is not None else None,
                    "branch": head,
                    "task_ref": task_ref_from_branch(head),
                    "actor": user.get("login") if isinstance(user, dict) else None,
                    "occurred_at": pr.get("updated_at") or pr.get("created_at"),
                    "payload": {
                        "number": number,
                        "state": pr.get("state"),
                        "merged": bool(pr.get("merged_at")),
                        "base": (pr.get("base") or {}).get("ref"),
                        "head": head,
                    },
                }
            )
        for issue in self.list_issues():
            if issue.get("pull_request"):
                continue
            number = issue.get("number")
            labels = [
                str(label.get("name"))
                for label in (issue.get("labels") or [])
                if isinstance(label, dict) and label.get("name")
            ]
            ref = f"#{number}" if number is not None else None
            occurred = issue.get("updated_at") or issue.get("created_at")
            actor = (issue.get("user") or {}).get("login")
            items.append(
                {
                    "kind": "issue",
                    "ref": ref,
                    "task_ref": ref,
                    "branch": None,
                    "actor": actor,
                    "occurred_at": occurred,
                    "payload": {
                        "number": number,
                        "state": issue.get("state"),
                        "labels": sorted(labels),
                    },
                }
            )
            for label, kind in (
                ("paid-task", "task"),
                ("accepted", "acceptance"),
                ("dispute", "dispute_link"),
            ):
                if label in labels:
                    items.append(
                        {
                            "kind": kind,
                            "ref": ref,
                            "task_ref": ref,
                            "branch": None,
                            "actor": actor,
                            "occurred_at": occurred,
                            "payload": {"number": number},
                        }
                    )
        return items
