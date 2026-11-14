"""CLI partner-connector: backfill фактов из GitHub/Forgejo/Gitea."""
from __future__ import annotations

import typer
from rich.console import Console

from partner_cli.console_io import configure_stdio

from .base import ConnectorAPIError
from .forgejo import ForgejoConnector, GiteaConnector
from .github import GitHubConnector
from .sync import SyncError, sync

configure_stdio()
app = typer.Typer(help="Партнер коннекторы: первичная синхронизация фактов репозитория")
console = Console()


@app.callback()
def main() -> None:
    """Первичная синхронизация фактов репозитория в сервер «Партнер»."""


@app.command("sync-repo")
def sync_repo(
    repo: str = typer.Option(..., "--repo", help="owner/name"),
    project: str = typer.Option("partner", "--project"),
    forge: str = typer.Option("github", "--forge", help="github | forgejo | gitea"),
    api_url: str = typer.Option("", "--api-url", help="Base URL инстанса Forgejo/Gitea"),
    forge_token: str = typer.Option("", "--forge-token", envvar="PARTNER_FORGE_TOKEN"),
    server_url: str = typer.Option("http://localhost:8000", "--server-url"),
    api_token: str = typer.Option("", "--token", envvar="PARTNER_API_TOKEN"),
) -> None:
    """Собрать ветки/PR/issues через API платформы и импортировать в сервер."""
    if forge == "github":
        connector = GitHubConnector(repo, token=forge_token, base_url=api_url)
    elif forge == "forgejo":
        if not api_url:
            console.print("[red]Для Forgejo нужен --api-url инстанса.[/red]")
            raise typer.Exit(1)
        connector = ForgejoConnector(repo, token=forge_token, base_url=api_url)
    elif forge == "gitea":
        if not api_url:
            console.print("[red]Для Gitea нужен --api-url инстанса.[/red]")
            raise typer.Exit(1)
        connector = GiteaConnector(repo, token=forge_token, base_url=api_url)
    else:
        console.print("[red]forge должен быть github | forgejo | gitea[/red]")
        raise typer.Exit(1)
    try:
        result = sync(
            connector, server_url=server_url, project_id=project, api_token=api_token
        )
    except (ConnectorAPIError, SyncError) as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Импорт: {result.get('status')}, фактов {result.get('facts', 0)}, "
        f"связано сессий {len(result.get('linked_sessions') or [])}.[/green]"
    )


if __name__ == "__main__":
    app()
