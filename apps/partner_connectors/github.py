"""GitHub REST API коннектор (backfill веток/PR/issues)."""
from __future__ import annotations

from .base import ForgeConnector


class GitHubConnector(ForgeConnector):
    forge = "github"

    def __init__(self, repo: str, *, base_url: str = "", **kwargs):
        super().__init__(
            repo, base_url=base_url or "https://api.github.com", **kwargs
        )

    def list_branches(self) -> list[dict]:
        return self._get(
            f"/repos/{self.repo}/branches", {"per_page": 100}
        ) or []

    def list_pull_requests(self) -> list[dict]:
        return self._get(
            f"/repos/{self.repo}/pulls", {"state": "all", "per_page": 100}
        ) or []

    def list_issues(self) -> list[dict]:
        return self._get(
            f"/repos/{self.repo}/issues", {"state": "all", "per_page": 100}
        ) or []
