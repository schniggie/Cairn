from __future__ import annotations

import logging
import platform
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.auth_helper.client import AuthHelperClient
from cairn.auth_helper.desktop import DesktopNotifier
from cairn.auth_helper.launcher import AuthLoginLauncher
from cairn.dispatcher.protocol.client import CairnClient
from cairn.server.models import AuthRequest

LOG = logging.getLogger(__name__)


def default_helper_id() -> str:
    """A stable identifier for this desktop helper: ``hostname-username``."""
    host = socket.gethostname() or "unknown-host"
    user = ""
    try:
        import getpass

        user = getpass.getuser()
    except Exception:
        user = ""
    return f"{host}-{user}" if user else host


@dataclass(slots=True)
class AuthHelperConfig:
    server: str
    config_path: Path
    helper_id: str
    project_id: str | None = None
    # Read from deployment environment; never place this opaque token in graph data.
    token: str | None = None
    poll_interval: float = 2.0
    notification: bool = True
    auto_launch: bool = True
    max_parallel_logins: int = 1
    claim_wait_seconds: float = 30.0
    claim_poll_interval: float = 0.2
    # Event-only helpers require the Dispatcher control-plane migration to be active.
    control_plane_mode: str = "dual_write"
    event_retry_attempts: int = 3


@dataclass(slots=True)
class _ActiveLogin:
    request: AuthRequest
    process: Any | None = None


class AuthHelperDaemon:
    """Poll loop for the desktop auth helper.

    Each tick it lists ``pending`` auth requests, attempts an atomic claim (so multiple
    helpers/dispatchers never pop up duplicate browsers), then notifies the operator and
    — if enabled — launches the headed login flow. ``max_parallel_logins`` caps how many
    login browsers may be open at once.
    """

    def __init__(
        self,
        config: AuthHelperConfig,
        client: AuthHelperClient | None = None,
        notifier: DesktopNotifier | None = None,
        launcher: AuthLoginLauncher | None = None,
    ):
        self.config = config
        if config.control_plane_mode == "legacy":
            raise RuntimeError(
                "auth-helper event mode requires auth_control_plane_mode=dual_write "
                "or enforced; legacy mode does not consume helper events"
            )
        if config.event_retry_attempts < 1:
            raise ValueError("event_retry_attempts must be at least 1")
        self._client = client or AuthHelperClient(CairnClient(config.server, server_token=config.token))
        self._notifier = notifier or DesktopNotifier(enabled=config.notification)
        self._launcher = launcher
        self._running = True
        self._active: dict[str, _ActiveLogin] = {}
        self._launch_requested: set[str] = set()

    def _launcher_for(self, request: AuthRequest) -> AuthLoginLauncher:
        if self._launcher is not None:
            return self._launcher
        return AuthLoginLauncher(
            config_path=self.config.config_path,
            project_id=request.project_id,
            target=request.auth_ref,
        )

    def run_once(self) -> None:
        if not self._running:
            return
        self._reap_finished()
        if self._active_process_count() >= self.config.max_parallel_logins:
            return
        if not self.config.project_id:
            LOG.warning("auth helper has no project scope; refusing global request discovery")
            return
        requests = self._client.list_pending(self.config.project_id)
        for request in requests:
            if not self._running:
                return
            if self._active_process_count() >= self.config.max_parallel_logins:
                return
            self._handle(request)
            self._reap_finished()

    def _active_process_count(self) -> int:
        return sum(entry.process is not None for entry in self._active.values())

    def _reap_finished(self) -> None:
        for request_id, entry in list(self._active.items()):
            if entry.process is None:
                continue
            try:
                returncode = entry.process.poll()
            except Exception as exc:
                LOG.warning("failed to poll auth login request=%s error=%s", request_id, exc)
                continue
            if returncode is not None:
                self._active.pop(request_id, None)

    def _handle(self, request: AuthRequest) -> None:
        # The request remains pending until Dispatcher consumes the event. Remember
        # accepted launches locally so a fast poll cannot open duplicate browsers.
        if request.id in self._launch_requested:
            return
        # Event submission is the sole Helper write. Dispatcher consumes the queued
        # launch event and performs the actual claim/state transition.
        if not self._client.submit_event(request, "launch_requested"):
            LOG.info("auth launch event was not accepted request=%s", request.id)
            return
        self._launch_requested.add(request.id)
        if not self._client.wait_until_claimed(
            request,
            actor_id=self.config.helper_id,
            timeout_seconds=self.config.claim_wait_seconds,
            poll_interval=self.config.claim_poll_interval,
        ):
            LOG.info("dispatcher did not claim auth request=%s before timeout", request.id)
            self._launch_requested.discard(request.id)
            return
        LOG.info("auth launch event acknowledged request=%s target=%s", request.id, request.auth_ref)
        self._active[request.id] = _ActiveLogin(request=request)
        self._notifier.notify_auth_required(request)
        if self.config.auto_launch:
            launcher = self._launcher_for(request)
            try:
                process = launcher.launch(request)
                self._active[request.id].process = process
                # Browser progress is event-only; Dispatcher applies the transition.
                if not self._submit_event_with_retry(request, "browser_opened"):
                    LOG.error(
                        "browser_opened event was not accepted after retries request=%s",
                        request.id,
                    )
                    self._stop_process(process)
                    self._submit_event_with_retry(request, "login_failed")
                    self._active.pop(request.id, None)
                    self._launch_requested.discard(request.id)
                    return
                LOG.info("auth login launched request=%s pid=%s", request.id, process.pid)
            except Exception as exc:
                LOG.warning("failed to launch auth login request=%s error=%s", request.id, exc)
                self._submit_event_with_retry(request, "login_failed")
                self._active.pop(request.id, None)
                self._launch_requested.discard(request.id)

    def _submit_event_with_retry(self, request: AuthRequest, kind: str) -> bool:
        for attempt in range(1, self.config.event_retry_attempts + 1):
            try:
                if self._client.submit_event(request, kind):
                    return True
            except Exception as exc:
                LOG.warning(
                    "auth event submission failed request=%s kind=%s attempt=%s/%s error=%s",
                    request.id, kind, attempt, self.config.event_retry_attempts, exc,
                )
            else:
                LOG.warning(
                    "auth event rejected request=%s kind=%s attempt=%s/%s",
                    request.id, kind, attempt, self.config.event_retry_attempts,
                )
        return False

    @staticmethod
    def _stop_process(process: Any) -> None:
        """Stop a browser whose launch cannot be represented in the graph."""
        try:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                terminate()
        except Exception as exc:
            LOG.warning("failed to stop untracked auth browser error=%s", exc)

    def run_forever(self) -> None:
        LOG.info("auth helper starting server=%s helper=%s", self.config.server, self.config.helper_id)
        while self._running:
            try:
                self.run_once()
            except Exception as exc:
                LOG.warning("auth helper tick failed error=%s", exc)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False
