"""CLI partner-agent: init, start, heartbeat, stop, run, status, mcp.

Кит пишет только факты времени; деньги считает сервер (partner_cli.calc).
"""
from __future__ import annotations

import pathlib

import typer
from rich.console import Console
from rich.table import Table

from partner_cli.console_io import configure_stdio

from .client import PartnerClient, PartnerError
from .config import AgentConfig, load_config, save_config
from .daemon import run_daemon
from .mcp_server import MCPServer
from .session import AgentSession, KitError

configure_stdio()
app = typer.Typer(help="Партнер agent-kit: сессии, heartbeat, офлайн-буфер, MCP")
console = Console()


def _load(path: pathlib.Path | None) -> AgentConfig:
    try:
        cfg = load_config(path)
        cfg.validate()
    except ValueError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)
    return cfg


def _fail(err: Exception) -> None:
    console.print(f"[red]{err}[/red]")
    raise typer.Exit(1)


@app.command()
def init(
    participant: str = typer.Option("", "--participant", help="ID участника"),
    server_url: str = typer.Option("http://localhost:8000", "--server-url"),
    project: str = typer.Option("partner", "--project"),
    token: str = typer.Option("", "--token", envvar="PARTNER_API_TOKEN"),
    timezone: str = typer.Option("Europe/Moscow", "--tz"),
    task: str = typer.Option("", "--task", help="Задача по умолчанию"),
    branch: str = typer.Option("", "--branch", help="Ветка по умолчанию"),
    device_label: str = typer.Option("", "--device-label"),
    no_device: bool = typer.Option(False, "--no-device", help="Не регистрировать устройство"),
    path: pathlib.Path = typer.Option(None, "--config", help="Путь к agent.yaml"),
) -> None:
    """Создать локальный конфиг; при возможности зарегистрировать устройство."""
    participant = participant or typer.prompt("ID участника")
    cfg = AgentConfig(
        server_url=server_url,
        api_token=token,
        participant_id=participant,
        project_id=project,
        timezone=timezone,
        task_ref=task or None,
        branch=branch or None,
    )
    if not no_device:
        try:
            device = PartnerClient(cfg.server_url, cfg.api_token).register_device(
                participant, device_label or "agent-kit"
            )
            cfg.device_id = device["device_id"]
            cfg.device_secret = device["secret"]
            console.print(
                f"[green]Устройство {cfg.device_id} зарегистрировано.[/green] "
                "Секрет сохранен только в локальном конфиге."
            )
        except PartnerError as err:
            console.print(
                f"[yellow]Устройство не зарегистрировано ({err}).[/yellow] "
                "Конфиг будет создан без подписи; повторите: partner-agent device-register"
            )
    target = save_config(cfg, path)
    console.print(f"[green]Конфиг: {target}[/green]")
    console.print(
        "Дальше: [bold]partner-agent start --task <id> --branch <ветка>[/bold], "
        "затем [bold]partner-agent run[/bold] или [bold]partner-agent mcp[/bold]"
    )


@app.command()
def device_register(
    label: str = typer.Option("agent-kit", "--label"),
    path: pathlib.Path = typer.Option(None, "--config"),
) -> None:
    """Зарегистрировать новое устройство и заменить секрет в конфиге."""
    cfg = _load(path)
    try:
        device = PartnerClient(cfg.server_url, cfg.api_token).register_device(
            cfg.participant_id, label
        )
    except PartnerError as err:
        _fail(err)
    cfg.device_id = device["device_id"]
    cfg.device_secret = device["secret"]
    save_config(cfg, path)
    console.print(f"[green]Устройство {cfg.device_id} сохранено в конфиг.[/green]")


@app.command()
def start(
    task: str = typer.Option("", "--task", help="Задача/issue"),
    branch: str = typer.Option("", "--branch", help="Ветка"),
    no_agent: bool = typer.Option(False, "--no-agent", help="Без ИИ-агента (track_only)"),
    path: pathlib.Path = typer.Option(None, "--config"),
) -> None:
    """Начать сессию учета (идемпотентно при активной сессии)."""
    cfg = _load(path)
    session = AgentSession(cfg)
    try:
        session_id = session.start(
            task_ref=task or None, branch=branch or None, agent_attached=not no_agent
        )
        result = session.flush()
    except (KitError, PartnerError) as err:
        _fail(err)
    console.print(f"[green]Сессия {session_id}[/green]")
    if result["pending"]:
        console.print(
            f"[yellow]Офлайн: {result['pending']} событий в буфере; "
            "отправятся при восстановлении сети (partner-agent flush).[/yellow]"
        )
    if result["failed"]:
        for item in result["failed"]:
            console.print(f"[yellow]Отклонено сервером: {item['key']} ({item['code']})[/yellow]")


@app.command()
def heartbeat(
    input_events: int = typer.Option(0, "--input", help="События ввода за интервал"),
    focus: int = typer.Option(0, "--focus", help="Фокус окна (0/1)"),
    tool_calls: int = typer.Option(0, "--tool-calls"),
    files_touched: int = typer.Option(0, "--files-touched"),
    commits: int = typer.Option(0, "--commits"),
    diffs: int = typer.Option(0, "--diffs"),
    agent_running: bool = typer.Option(True, "--agent-running/--no-agent-running"),
    idle_sec: float = typer.Option(-1.0, "--idle-sec", help="Секунд без ввода"),
    path: pathlib.Path = typer.Option(None, "--config"),
) -> None:
    """Отправить один интервал от конца предыдущего heartbeat до сейчас."""
    cfg = _load(path)
    session = AgentSession(cfg)
    signals = {
        "input_events": input_events,
        "focus": focus,
        "tool_calls": tool_calls,
        "files_touched": files_touched,
        "commits": commits,
        "diffs": diffs,
        "agent_running": agent_running,
    }
    try:
        events = session.heartbeat(signals, idle_sec=None if idle_sec < 0 else idle_sec)
        result = session.flush()
    except (KitError, PartnerError) as err:
        _fail(err)
    console.print(
        f"[green]Heartbeat: {len(events)} интервал(ов), буфер: {result['pending']}.[/green]"
    )


@app.command()
def pause(path: pathlib.Path = typer.Option(None, "--config")) -> None:
    """Пометить дальнейшие интервалы как простой."""
    cfg = _load(path)
    try:
        AgentSession(cfg).pause()
    except KitError as err:
        _fail(err)
    console.print("[yellow]Сессия на паузе.[/yellow]")


@app.command()
def resume(path: pathlib.Path = typer.Option(None, "--config")) -> None:
    """Снять паузу."""
    cfg = _load(path)
    try:
        AgentSession(cfg).resume()
    except KitError as err:
        _fail(err)
    console.print("[green]Сессия продолжена.[/green]")


@app.command()
def stop(path: pathlib.Path = typer.Option(None, "--config")) -> None:
    """Завершить сессию, отправив финальный интервал."""
    cfg = _load(path)
    try:
        result = AgentSession(cfg).stop()
    except (KitError, PartnerError) as err:
        _fail(err)
    console.print(
        f"[green]Сессия завершена. Отправлено: {result['sent']}, "
        f"в буфере: {result['pending']}.[/green]"
    )


@app.command()
def run(
    task: str = typer.Option("", "--task"),
    branch: str = typer.Option("", "--branch"),
    no_agent: bool = typer.Option(False, "--no-agent"),
    interval: int = typer.Option(0, "--interval", help="Секунд между heartbeat"),
    iterations: int = typer.Option(0, "--iterations", help="Ограничить число тиков"),
    path: pathlib.Path = typer.Option(None, "--config"),
) -> None:
    """Цикл heartbeat (daemon). Ctrl+C — финальный интервал и выход."""
    cfg = _load(path)
    try:
        result = run_daemon(
            cfg,
            task_ref=task or None,
            branch=branch or None,
            agent_attached=not no_agent,
            interval=interval or None,
            iterations=iterations or None,
        )
    except (KitError, PartnerError) as err:
        console.print(
            f"[yellow]{err}[/yellow]\n"
            "События остаются в офлайн-буфере; отправьте позже: partner-agent flush"
        )
        raise typer.Exit(1)
    console.print(
        f"[green]Сессия {result['session_id']} завершена: "
        f"{result['ticks']} тиков, {result['heartbeats']} heartbeat, "
        f"в буфере {result['flush']['pending']}.[/green]"
    )


@app.command()
def flush(path: pathlib.Path = typer.Option(None, "--config")) -> None:
    """Отправить накопленный офлайн-буфер в порядке seq."""
    cfg = _load(path)
    try:
        result = AgentSession(cfg).flush()
    except PartnerError as err:
        _fail(err)
    console.print(
        f"[green]Отправлено: {result['sent']}; осталось: {result['pending']}.[/green]"
    )
    for item in result["failed"]:
        console.print(f"[yellow]Отклонено: {item['key']} ({item['code']})[/yellow]")


@app.command()
def status(
    month: str = typer.Option("", "--month", help="YYYY-MM"),
    local_only: bool = typer.Option(False, "--local", help="Только локальное состояние"),
    path: pathlib.Path = typer.Option(None, "--config"),
) -> None:
    """Локальная сессия и месячные часы участника."""
    cfg = _load(path)
    session = AgentSession(cfg)
    local = session.status()
    table = Table(title="Локальная сессия")
    table.add_column("Поле")
    table.add_column("Значение")
    for key in (
        "session_id", "participant_id", "project_id", "task_ref", "branch",
        "started_at_utc", "last_end_utc", "seq", "paused", "pending_events",
        "device_id",
    ):
        table.add_row(key, str(local[key]))
    console.print(table)
    if local_only:
        return
    try:
        remote = PartnerClient(cfg.server_url, cfg.api_token).get_status(
            cfg.participant_id, month or None
        )
    except PartnerError as err:
        console.print(f"[yellow]Сервер: {err}[/yellow]")
        return
    console.print(
        f"Месяц {remote['month']}: {remote['paid_hours_month']} ч, "
        f"{remote['amount_rub_month']} ₽, доля {remote['share_bp_month']} bp"
    )


@app.command()
def day(
    date: str = typer.Argument(..., help="YYYY-MM-DD"),
    path: pathlib.Path = typer.Option(None, "--config"),
) -> None:
    """Итог дня участника с сервера."""
    cfg = _load(path)
    try:
        data = PartnerClient(cfg.server_url, cfg.api_token).get_day(
            date, cfg.participant_id
        )
    except PartnerError as err:
        _fail(err)
    console.print(
        f"{data['date']}: {data['paid_hours']} оплач. ч, "
        f"{data['amount_rub']} ₽, флаги: {', '.join(data.get('flags') or []) or '—'}"
    )


@app.command()
def explain(
    date: str = typer.Argument(..., help="YYYY-MM-DD"),
    path: pathlib.Path = typer.Option(None, "--config"),
) -> None:
    """Раскладка начисления дня с сервера."""
    cfg = _load(path)
    try:
        data = PartnerClient(cfg.server_url, cfg.api_token).explain_day(
            date, cfg.participant_id
        )
    except PartnerError as err:
        _fail(err)
    console.print_json(data=data)


@app.command()
def show_config(path: pathlib.Path = typer.Option(None, "--config")) -> None:
    """Показать конфиг без секретов."""
    cfg = _load(path)
    console.print_json(data=cfg.redacted())


@app.command()
def mcp(path: pathlib.Path = typer.Option(None, "--config")) -> None:
    """MCP-сервер (stdio, JSON-RPC): инструменты для ИИ-агента."""
    cfg = _load(path)
    MCPServer(cfg).serve_stdio()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
