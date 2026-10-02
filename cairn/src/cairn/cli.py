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


@main.group()
def auth():
    """Manage real-environment login sessions (login / verify / list / remove)."""


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
@click.option("--target", "target_name", required=True, help="Auth target name (auth_ref)")
@click.option("--request", "request_id", required=False, help="Auth request id to drive (auth_007)")
def login(config_path: Path, project_id: str, target_name: str, request_id: str | None):
    """Open a headed browser for the operator to log in, then save the session state."""
    from cairn.auth.manager import AuthManager
    from cairn.auth.models import AuthMeta, utcnow
    from cairn.auth.store import AuthStore
    from cairn.auth.verifier import AuthVerifier
    from cairn.dispatcher.config import DispatchConfig
    from cairn.auth_helper.client import AuthHelperClient
    from cairn.dispatcher.protocol.client import CairnClient
    import os

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    if request_id is not None and getattr(config, "auth_control_plane_mode", None) == "legacy":
        raise click.ClickException(
            "request-bound auth login requires auth_control_plane_mode=dual_write or enforced"
        )
    try:
        target = config.auth.target(target_name)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    event_client: AuthHelperClient | None = None
    if request_id is not None:
        token = os.environ.get(config.auth.helper_token_env)
        event_client = AuthHelperClient(CairnClient(config.server, server_token=token))

    def _submit(kind: str, capture_generation: int | None = None) -> bool:
        if event_client is None or request_id is None:
            return True
        return event_client.submit_event_fields(
            project_id,
            request_id,
            target.name,
            kind,
            capture_generation=capture_generation,
        )

    try:
        if request_id is not None and not event_client.wait_until_claimed_fields(
            project_id,
            request_id,
            actor_id=getattr(config.auth, "helper_actor_id", None),
        ):
            raise click.ClickException("dispatcher did not claim auth request")
        if request_id is not None and not _submit("browser_opened"):
            raise click.ClickException("failed to submit browser_opened event")
        store = AuthStore(Path(config.auth.store_root))
        manager = AuthManager()
        result = manager.capture_interactive(
            target,
            login_timeout_seconds=config.auth.login_timeout,
            verify_timeout_seconds=config.auth.verify_timeout,
            on_status=lambda msg: click.echo(f"[auth] {msg}"),
        )
        if result.storage_state is None:
            _submit("login_failed")
            raise click.ClickException("login did not produce a verified session")

        # Re-verify the freshly saved state independently before trusting it.
        verifier = AuthVerifier(timeout_ms=config.auth.verify_timeout * 1000)
        verification = verifier.verify_storage_state(target, result.storage_state)
        if not verification.valid:
            _submit("login_failed")
            raise click.ClickException(f"re-verification failed: {verification.reason}")
        manifest = store.write_capture(
            project_id,
            target.name,
            result.storage_state,
            request_id=request_id or "local",
            actor_id=getattr(config.auth, "helper_actor_id", "local"),
        )
        state_path = store.state_file(project_id, target.name)
        click.echo(f"[auth] saved storage state: {state_path}")

        store.write_meta(
            project_id,
            target.name,
            AuthMeta(
                target=target.name,
                role=target.role,
                base_url=target.base_url,
                created_at=utcnow(),
                verified_at=utcnow(),
                verification={
                    "page": verification.page_ok,
                    "selector": verification.selector_ok,
                    "api": verification.api_ok,
                },
            ),
        )

        if request_id is not None:
            if not _submit("login_succeeded", manifest.capture_generation):
                raise click.ClickException("failed to submit login_succeeded event")
        click.echo("[auth] authenticated session captured locally")
        click.echo(f"[auth] target={target.name} role={target.role} verification={'+'.join(verification.methods())}")
    finally:
        if event_client is not None:
            event_client.client.close()


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
@click.option("--target", "target_name", required=True, help="Auth target name (auth_ref)")
@click.option("--request", "request_id", required=False, help="Auth request id to drive (auth_007)")
def verify(config_path: Path, project_id: str, target_name: str, request_id: str | None):
    """Load an existing saved session state and re-verify it is still valid."""
    from cairn.auth.models import AuthMeta, utcnow
    from cairn.auth.store import AuthStore
    from cairn.auth.verifier import AuthVerifier
    from cairn.auth_helper.client import AuthHelperClient
    from cairn.dispatcher.config import DispatchConfig
    from cairn.dispatcher.protocol.client import CairnClient
    import os

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    if request_id is not None and getattr(config, "auth_control_plane_mode", None) == "legacy":
        raise click.ClickException(
            "request-bound auth verify requires auth_control_plane_mode=dual_write or enforced"
        )
    try:
        target = config.auth.target(target_name)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    store = AuthStore(Path(config.auth.store_root))
    try:
        storage_state = store.load_state(project_id, target.name)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    capture_manifest = None
    if request_id is not None:
        actor_id = getattr(config.auth, "helper_actor_id", "helper")
        try:
            capture_manifest = store.load_manifest(project_id, target.name)
            store.validate_capture(
                project_id,
                target.name,
                request_id=request_id,
                actor_id=actor_id,
                capture_generation=capture_manifest.capture_generation,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise click.ClickException(f"capture validation failed: {exc}") from exc

    verifier = AuthVerifier(timeout_ms=config.auth.verify_timeout * 1000)
    verification = verifier.verify_storage_state(target, storage_state)

    event_client: AuthHelperClient | None = None
    if request_id is not None:
        token = os.environ.get(config.auth.helper_token_env)
        event_client = AuthHelperClient(CairnClient(config.server, server_token=token))
        try:
            if not event_client.wait_until_verifiable_fields(
                project_id, request_id, actor_id=actor_id,
            ):
                raise click.ClickException("dispatcher did not claim auth request")
            kind = "login_succeeded" if verification.valid else "login_failed"
            if not event_client.submit_event_fields(
                project_id,
                request_id,
                target.name,
                kind,
                capture_generation=capture_manifest.capture_generation if verification.valid else None,
            ):
                raise click.ClickException(f"failed to submit {kind} event")
        finally:
            event_client.client.close()

    # Refresh meta with the latest verification outcome.
    try:
        meta = store.load_meta(project_id, target.name)
    except FileNotFoundError:
        meta = AuthMeta(
            target=target.name,
            role=target.role,
            base_url=target.base_url,
            created_at=utcnow(),
        )
    meta.verified_at = utcnow()
    meta.verification = {
        "page": verification.page_ok,
        "selector": verification.selector_ok,
        "api": verification.api_ok,
    }
    store.write_meta(project_id, target.name, meta)
    if verification.valid:
        click.echo(f"[auth] session valid target={target.name} role={target.role} verification={'+'.join(verification.methods())}")
    else:
        click.echo(f"[auth] session invalid target={target.name}")


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
def list(config_path: Path, project_id: str):
    """List saved auth profiles for a project."""
    from cairn.auth.store import AuthStore
    from cairn.dispatcher.config import DispatchConfig

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    store = AuthStore(Path(config.auth.store_root))
    refs = store.list_profiles(project_id)
    if not refs:
        click.echo("[auth] no saved auth profiles")
        return
    for ref in refs:
        try:
            meta = store.load_meta(project_id, ref)
            click.echo(f"{ref}\trole={meta.role}\tverified_at={meta.verified_at}")
        except FileNotFoundError:
            click.echo(f"{ref}\t(no meta)")


@auth.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--project", "project_id", required=True, help="Project id (e.g. proj_001)")
@click.option("--target", "target_name", required=True, help="Auth target name (auth_ref)")
def remove(config_path: Path, project_id: str, target_name: str):
    """Remove a saved auth profile for a project."""
    from cairn.auth.store import AuthStore
    from cairn.dispatcher.config import DispatchConfig

    config = DispatchConfig.load(config_path)
    if config.auth is None:
        raise click.ClickException("dispatch config has no 'auth' section")
    store = AuthStore(Path(config.auth.store_root))
    removed = store.remove_profile(project_id, target_name)
    click.echo(f"[auth] {'removed' if removed else 'not found'}: {target_name}")



@main.command()
@click.option(
    "--server",
    default="http://localhost:8000",
    show_default=True,
    help="Cairn server base URL",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path (contains the auth targets)",
)
@click.option("--project", "project_id", required=True, help="Project scope for Helper request discovery")
@click.option("--helper-name", "helper_id", default=None, help="Helper identifier (default hostname-username)")
@click.option("--token-env", default="CAIRN_AUTH_HELPER_TOKEN", show_default=True, help="Environment variable containing the helper bearer token")
@click.option("--poll-interval", type=float, default=2.0, show_default=True, help="Poll interval in seconds")
@click.option("--notification/--no-notification", default=True, show_default=True, help="Enable desktop notifications")
@click.option("--auto-launch/--no-auto-launch", default=True, show_default=True, help="Auto-launch the headed login browser")
@click.option("--max-parallel", "max_parallel", type=int, default=1, show_default=True, help="Max concurrent login browsers")
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def auth_helper(
    server: str,
    config_path: Path,
    project_id: str,
    helper_id: str | None,
    token_env: str,
    poll_interval: float,
    notification: bool,
    auto_launch: bool,
    max_parallel: int,
    log_level: str,
):
    """Run the desktop auth helper (polls pending auth requests and launches login)."""
    from cairn.auth_helper.daemon import (
        AuthHelperConfig,
        AuthHelperDaemon,
        default_helper_id,
    )

    configure_logging(log_level)
    import os

    from cairn.dispatcher.config import DispatchConfig

    dispatch_config = DispatchConfig.load(config_path)

    config = AuthHelperConfig(
        server=server,
        config_path=config_path,
        helper_id=helper_id or default_helper_id(),
        project_id=project_id,
        token=os.environ.get(token_env),
        poll_interval=poll_interval,
        notification=notification,
        auto_launch=auto_launch,
        max_parallel_logins=max_parallel,
        control_plane_mode=dispatch_config.auth_control_plane_mode,
    )
    daemon = AuthHelperDaemon(config)
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        daemon.stop()
