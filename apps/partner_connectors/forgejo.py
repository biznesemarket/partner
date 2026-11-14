"""Forgejo / Gitea API коннектор (backfill веток/PR/issues).

Forgejo и Gitea совместимы по REST API /api/v1/repos/{owner}/{repo};
различие — только forge в URL импорта и заголовке вебхука.
"""
from __future__ import annotations

from .base import ForgeConnector


class ForgejoConnector(ForgeConnector):
    forge = "forgejo"

    def __init__(self, repo: str, *, base_url: str, **kwargs):
        super().__init__(repo, base_url=base_url, **kwargs)

    def list_branches(self) -> list[dict]:
        return self._get(
            f"/api/v1/repos/{self.repo}/branches", {"limit": 100}
        ) or []

    def list_pull_requests(self) -> list[dict]:
        return self._get(
            f"/api/v1/repos/{self.repo}/pulls", {"state": "all", "limit": 100}
        ) or []

    def list_issues(self) -> list[dict]:
        return self._get(
            f"/api/v1/repos/{self.repo}/issues",
            {"state": "all", "type": "issues", "limit": 100},
        ) or []


class GiteaConnector(ForgejoConnector):
    forge = "gitea"
