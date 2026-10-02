from pathlib import Path

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
@click.option(
    "--dispatch-config",
    type=click.Path(path_type=Path),
    default="dispatch.yaml",
    show_default=True,
    help="Dispatcher YAML exposed through the local admin API",
)
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool, dispatch_config: Path):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.dispatch_config_store import configure_dispatch_config

    configure_dispatch_config(dispatch_config)
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("research-worker")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=db.DEFAULT_DB,
    show_default=True,
    help="SQLite database path",
)
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Root directory for per-session research workspaces",
)
@click.option(
    "--interval",
    "interval_seconds",
    type=click.IntRange(min=2, max=3600),
    default=5,
    show_default=True,
    help="Seconds between claim ticks",
)
@click.option(
    "--lease-seconds",
    "lease_seconds",
    type=click.IntRange(min=30, max=3600),
    default=120,
    show_default=True,
    help="Research session lease duration",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
@click.option("--once", is_flag=True, help="Run one claim tick and exit")
@click.option(
    "--driver",
    default=None,
    show_default=False,
    help="Research agent driver (claudecode|codex|pi|mock); default claudecode, env CAIRN_RESEARCH_DRIVER",
)
def research_worker(
    db_path: Path,
    workspace_root: Path | None,
    interval_seconds: int,
    lease_seconds: int,
    log_level: str,
    once: bool,
    driver: str | None,
):
    """Run the bounded autonomous research worker."""
    configure_logging(log_level)
    from cairn.server.research_worker import ResearchWorker

    worker = ResearchWorker(
        db_path=db_path,
        workspace_root=workspace_root,
        interval_seconds=interval_seconds,
        lease_seconds=lease_seconds,
        driver=driver,
    )
    worker.run(once=once)


@main.command()
@click.option(
    "--server",
    default="http://127.0.0.1:8000",
    show_default=True,
    help="Cairn server base URL",
)
@click.option("--once", is_flag=True, help="Run one poll round and exit")
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def ctf_bridge(server: str, once: bool, log_level: str):
    """Run the CTF platform bridge."""
    configure_logging(log_level)
    from cairn.ctfbridge.bridge import CtfBridge

    bridge = CtfBridge(server)
    if once:
        bridge.run_once()
    else:
        bridge.run()
