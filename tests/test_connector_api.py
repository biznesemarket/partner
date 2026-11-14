"""Тесты backfill-коннекторов: нормализация API GitHub/Forgejo в факты."""
from __future__ import annotations

from partner_connectors.forgejo import ForgejoConnector
from partner_connectors.github import GitHubConnector


def test_github_import_items_normalization():
    def transport(url: str, headers: dict, timeout: float):
        assert headers["Authorization"] == "token t"
        if "/branches" in url:
            return [{"name": "dev/dev1/T-1", "commit": {"sha": "a" * 40}}]
        if "/pulls" in url:
            return [{
                "number": 2, "state": "closed", "merged_at": "2026-11-12T12:00:00Z",
                "created_at": "2026-11-12T09:00:00Z",
                "head": {"ref": "dev/dev1/T-2"}, "base": {"ref": "main"},
                "user": {"login": "dev1"},
            }]
        if "/issues" in url:
            return [
                {"number": 3, "state": "open", "user": {"login": "dev1"},
                 "labels": [{"name": "paid-task"}],
                 "created_at": "2026-11-12T10:00:00Z"},
                {"number": 4, "state": "open", "pull_request": {"url": "x"},
                 "labels": []},
            ]
        raise AssertionError(url)

    items = GitHubConnector("acme/partner", token="t", transport=transport).import_items()
    kinds = [item["kind"] for item in items]
    assert kinds == ["branch", "pull_request", "issue", "task"]
    assert items[0]["task_ref"] == "T-1"
    assert items[1]["task_ref"] == "T-2"
    assert items[2]["ref"] == "#3"
    assert items[3]["kind"] == "task"


def test_forgejo_endpoints_and_items():
    urls: list[str] = []

    def transport(url: str, headers: dict, timeout: float):
        urls.append(url)
        if "/branches" in url:
            return [{"name": "dev/dev1/T-5", "commit": {"id": "b" * 40}}]
        return []

    connector = ForgejoConnector(
        "acme/forge", base_url="https://forge.example", token="t",
        transport=transport,
    )
    items = connector.import_items()
    assert items[0]["kind"] == "branch"
    assert items[0]["payload"]["commit"] == "b" * 40
    assert all("/api/v1/repos/acme/forge/" in url for url in urls)
