"""noted CLI: init, login, sync, daemon, status."""

import logging
import sys
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console

from noted_cli.api_client import ApiError, SupernoteApiClient
from noted_cli.config import ClientConfig
from noted_cli.daemon import SyncDaemon
from noted_cli.engine import POLICIES, SyncEngine, SyncResult
from noted_cli.state_db import StateDB

app = typer.Typer(help="Keep a local directory in sync with a Supernote sync server.")
console = Console()

ConfigDirOpt = Annotated[
    Path | None, typer.Option("--config-dir", help="Override the config directory")
]


def _load(config_dir: Path | None) -> ClientConfig:
    return ClientConfig.load(config_dir)


def _connect(config: ClientConfig) -> SupernoteApiClient:
    http = httpx.Client(base_url=config.server_url, timeout=120)
    api = SupernoteApiClient(http, config.equipment_no)
    api.token = config.load_token()
    if api.token is None or not api.validate_token():
        console.print("[red]Not logged in (or token expired). Run: noted login[/red]")
        raise typer.Exit(2)
    return api


@app.command()
def init(
    server: Annotated[str, typer.Option(help="Server base URL")],
    account: Annotated[str, typer.Option(help="Account email")],
    directory: Annotated[Path, typer.Option("--dir", help="Local directory to sync")],
    conflict_policy: Annotated[
        str, typer.Option(help=f"One of: {', '.join(POLICIES)}")
    ] = "keep-both",
    poll_interval: Annotated[int, typer.Option(help="Daemon poll interval (seconds)")] = 60,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Write the client configuration."""
    if conflict_policy not in POLICIES:
        console.print(f"[red]conflict-policy must be one of: {', '.join(POLICIES)}[/red]")
        raise typer.Exit(2)
    config = _load(config_dir)
    config.server_url = server.rstrip("/")
    config.account = account
    config.sync_root = directory.expanduser().resolve()
    config.conflict_policy = conflict_policy
    config.poll_interval_seconds = poll_interval
    config.save()
    console.print(f"Config written to {config.config_file}")
    console.print(f"Equipment number: [bold]{config.equipment_no}[/bold]")


@app.command()
def login(
    config_dir: ConfigDirOpt = None,
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help="Read password from stdin")
    ] = False,
) -> None:
    """Authenticate and cache an access token (the password is not stored)."""
    config = _load(config_dir)
    if not config.account:
        console.print("[red]No account configured. Run: noted init[/red]")
        raise typer.Exit(2)
    password = (
        sys.stdin.readline().rstrip("\n")
        if password_stdin
        else typer.prompt("Password", hide_input=True)
    )
    http = httpx.Client(base_url=config.server_url, timeout=30)
    api = SupernoteApiClient(http, config.equipment_no)
    try:
        token = api.login(config.account, password)
    except (httpx.HTTPError, ApiError) as exc:
        console.print(f"[red]Login failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    config.save_token(token)
    console.print("[green]Logged in.[/green]")


def _print_result(result: SyncResult) -> None:
    for rel in result.uploaded:
        console.print(f"  [cyan]up[/cyan]   {rel}")
    for rel in result.downloaded:
        console.print(f"  [green]down[/green] {rel}")
    for rel in result.deleted_local + result.deleted_remote:
        console.print(f"  [red]del[/red]  {rel}")
    for rel in result.conflicts:
        console.print(f"  [yellow]conflict[/yellow] {rel}")
    if result.changed == 0 and not result.conflicts:
        console.print("  up to date")


def _make_engine(config: ClientConfig, api: SupernoteApiClient) -> SyncEngine:
    return SyncEngine(
        api,
        StateDB(config.state_db_file),
        config.sync_root,
        conflict_policy=config.conflict_policy,
    )


@app.command()
def sync(config_dir: ConfigDirOpt = None) -> None:
    """Run a single bidirectional sync pass and exit."""
    config = _load(config_dir)
    api = _connect(config)
    result = _make_engine(config, api).run_once()
    _print_result(result)
    if result.conflicts and config.conflict_policy == "keep-both":
        raise typer.Exit(3)


@app.command()
def daemon(config_dir: ConfigDirOpt = None) -> None:
    """Watch the local directory and poll the server, syncing continuously."""
    logging.basicConfig(level=logging.INFO)
    config = _load(config_dir)
    api = _connect(config)
    engine = _make_engine(config, api)
    console.print(
        f"Watching [bold]{config.sync_root}[/bold], polling every "
        f"{config.poll_interval_seconds}s. Ctrl-C to stop."
    )
    sync_daemon = SyncDaemon(
        engine,
        config.poll_interval_seconds,
        on_result=lambda r: _print_result(r) if r.changed or r.conflicts else None,
    )
    try:
        sync_daemon.run()
    except KeyboardInterrupt:
        sync_daemon.stop()
        console.print("Stopped.")


@app.command()
def status(config_dir: ConfigDirOpt = None) -> None:
    """Show configuration and connectivity."""
    config = _load(config_dir)
    console.print(f"Server:          {config.server_url}")
    console.print(f"Account:         {config.account or '[not configured]'}")
    console.print(f"Sync root:       {config.sync_root}")
    console.print(f"Equipment:       {config.equipment_no}")
    console.print(f"Conflict policy: {config.conflict_policy}")
    http = httpx.Client(base_url=config.server_url, timeout=10)
    api = SupernoteApiClient(http, config.equipment_no)
    reachable = api.ping()
    console.print(f"Server reachable: {'[green]yes[/green]' if reachable else '[red]no[/red]'}")
    if reachable:
        api.token = config.load_token()
        valid = api.token is not None and api.validate_token()
        console.print(f"Token valid:      {'[green]yes[/green]' if valid else '[red]no[/red]'}")


if __name__ == "__main__":
    app()
