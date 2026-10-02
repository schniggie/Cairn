from __future__ import annotations

from pathlib import Path

import pytest

from cairn.auth_helper.client import AuthHelperClient, AuthHelperRequest
from cairn.dispatcher.protocol.client import ApiResult
from cairn.auth_helper.daemon import (
    AuthHelperConfig,
    AuthHelperDaemon,
    default_helper_id,
)
from cairn.auth_helper.desktop import DesktopNotifier
from cairn.server.models import AuthRequest


def _request(request_id: str = "auth_001") -> AuthRequest:
    return AuthRequest(
        id=request_id,
        project_id="proj_001",
        source_fact_ids=["f001"],
        auth_ref="target-user",
        role="user",
        reason="orders need auth",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
    )


class _FakeHelperClient:
    def __init__(self, pending: list[AuthRequest] | None = None) -> None:
        self.pending = pending or []
        self.claimed: list[tuple[str, str]] = []
        self.events: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self.failed: list[tuple[str, str | None]] = []
        self._claim_result = True
        self._event_result = True

    def list_pending(self, project_id: str | None = None) -> list[AuthRequest]:
        return list(self.pending)

    def claim(self, request_id: str, helper_id: str) -> bool:
        self.claimed.append((request_id, helper_id))
        return self._claim_result

    def submit_event(self, request: AuthRequest, kind: str, **_kwargs: object) -> bool:
        self.events.append((request.id, kind))
        return self._event_result

    def wait_until_claimed(self, request: AuthRequest, **_kwargs: object) -> bool:
        return True

    def mark_waiting(self, request_id: str) -> bool:
        return True

    def mark_verifying(self, request_id: str) -> bool:
        return True

    def mark_complete(self, request_id: str) -> bool:
        self.completed.append(request_id)
        return True

    def mark_fail(self, request_id: str, reason: str | None = None) -> bool:
        self.failed.append((request_id, reason))
        return True


class _FakeNotifier(DesktopNotifier):
    def __init__(self) -> None:
        super().__init__(enabled=True)
        self.notified: list[AuthRequest] = []

    def notify_auth_required(self, request: AuthRequest) -> None:
        self.notified.append(request)


class _FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[AuthRequest] = []
        self.processes: list[_FakeProcess] = []

    def launch(self, request: AuthRequest):
        self.launched.append(request)
        return self.processes.pop(0) if self.processes else _FakeProcess()


class _FakeProcess:
    pid = 1234

    def __init__(self, returncode: int | None = None, poll_error: Exception | None = None) -> None:
        self.returncode = returncode
        self.poll_error = poll_error

    def poll(self) -> int | None:
        if self.poll_error is not None:
            raise self.poll_error
        return self.returncode


class _TerminableProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class _FailingLauncher(_FakeLauncher):
    def launch(self, request: AuthRequest):
        self.launched.append(request)
        raise RuntimeError("cannot launch")


class _RetryingEventClient(_FakeHelperClient):
    def __init__(self, pending: list[AuthRequest]) -> None:
        super().__init__(pending)
        self.attempts: dict[str, int] = {}

    def submit_event(self, request: AuthRequest, kind: str, **_kwargs: object) -> bool:
        self.events.append((request.id, kind))
        self.attempts[kind] = self.attempts.get(kind, 0) + 1
        return kind not in {"browser_opened", "login_failed"}


def _config(tmp_path: Path) -> AuthHelperConfig:
    return AuthHelperConfig(
        server="http://localhost:8000",
        config_path=tmp_path / "dispatch.yaml",
        helper_id="desktop-test",
        project_id="proj_001",
        auto_launch=False,
        control_plane_mode="dual_write",
    )


def test_helper_rejects_legacy_control_plane_mode_at_startup(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.control_plane_mode = "legacy"
    with pytest.raises(RuntimeError, match="dual_write|enforced|legacy"):
        AuthHelperDaemon(config)


def test_helper_config_injects_its_own_bearer_token(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.token = "helper-secret"
    daemon = AuthHelperDaemon(config)
    try:
        assert daemon._client.client._session().headers["Authorization"] == "Bearer helper-secret"
    finally:
        daemon._client.client.close()


def test_default_helper_id_is_hostname_username() -> None:
    helper_id = default_helper_id()
    assert helper_id
    assert "unknown" not in helper_id or helper_id == "unknown-host"


def test_daemon_submits_launch_event_and_notifies(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request()])
    notifier = _FakeNotifier()
    daemon = AuthHelperDaemon(
        _config(tmp_path),
        client=client,
        notifier=notifier,
        launcher=_FakeLauncher(),
    )

    daemon.run_once()

    assert client.events == [("auth_001", "launch_requested")]
    assert len(notifier.notified) == 1
    assert notifier.notified[0].auth_ref == "target-user"


def test_daemon_auto_launch(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request()])
    launcher = _FakeLauncher()
    config = _config(tmp_path)
    config.auto_launch = True
    daemon = AuthHelperDaemon(
        config,
        client=client,
        notifier=_FakeNotifier(),
        launcher=launcher,
    )

    daemon.run_once()

    assert len(launcher.launched) == 1
    assert client.events == [("auth_001", "launch_requested"), ("auth_001", "browser_opened")]
    assert launcher.launched[0].id == "auth_001"


def test_daemon_retries_progress_events_and_reports_failure(tmp_path: Path) -> None:
    client = _RetryingEventClient(pending=[_request()])
    launcher = _FakeLauncher()
    config = _config(tmp_path)
    config.auto_launch = True
    config.event_retry_attempts = 3
    daemon = AuthHelperDaemon(
        config, client=client, notifier=_FakeNotifier(), launcher=launcher,
    )

    daemon.run_once()

    assert client.attempts["browser_opened"] == 3
    assert client.attempts["login_failed"] == 3
    assert daemon._active == {}


def test_daemon_stops_browser_when_progress_event_cannot_be_accepted(tmp_path: Path) -> None:
    client = _RetryingEventClient(pending=[_request()])
    process = _TerminableProcess()
    launcher = _FakeLauncher()
    launcher.processes = [process]
    config = _config(tmp_path)
    config.auto_launch = True
    daemon = AuthHelperDaemon(
        config, client=client, notifier=_FakeNotifier(), launcher=launcher,
    )

    daemon.run_once()

    assert process.terminated is True


def test_daemon_skips_when_launch_event_rejected(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request()])
    client._event_result = False
    launcher = _FakeLauncher()
    daemon = AuthHelperDaemon(
        _config(tmp_path),
        client=client,
        notifier=_FakeNotifier(),
        launcher=launcher,
    )

    daemon.run_once()

    # Event rejected -> no notification, no launch (avoids duplicate popups).
    assert client.events == [("auth_001", "launch_requested")]
    assert len(launcher.launched) == 0


def test_daemon_respects_max_parallel(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request("auth_001"), _request("auth_002")])
    launcher = _FakeLauncher()
    config = _config(tmp_path)
    config.auto_launch = True
    config.max_parallel_logins = 1
    daemon = AuthHelperDaemon(
        config,
        client=client,
        notifier=_FakeNotifier(),
        launcher=launcher,
    )

    daemon.run_once()

    # Only one request handled per tick when max_parallel_logins == 1.
    assert len(launcher.launched) == 1


def test_exited_process_is_reclaimed_before_admitting_pending_request(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request("auth_001"), _request("auth_002")])
    launcher = _FakeLauncher()
    launcher.processes = [_FakeProcess(returncode=0), _FakeProcess()]
    config = _config(tmp_path)
    config.auto_launch = True
    config.max_parallel_logins = 1
    daemon = AuthHelperDaemon(config, client=client, notifier=_FakeNotifier(), launcher=launcher)

    daemon.run_once()

    assert [request.id for request in launcher.launched] == ["auth_001", "auth_002"]
    assert set(daemon._active) == {"auth_002"}


def test_running_process_remains_active_and_blocks_capacity(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request("auth_001"), _request("auth_002")])
    launcher = _FakeLauncher()
    launcher.processes = [_FakeProcess()]
    config = _config(tmp_path)
    config.auto_launch = True
    config.max_parallel_logins = 1
    daemon = AuthHelperDaemon(config, client=client, notifier=_FakeNotifier(), launcher=launcher)

    daemon.run_once()
    daemon.run_once()

    assert [request.id for request in launcher.launched] == ["auth_001"]
    assert set(daemon._active) == {"auth_001"}


def test_launcher_poll_failure_keeps_entry_active_without_crashing(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request("auth_001"), _request("auth_002")])
    launcher = _FakeLauncher()
    launcher.processes = [_FakeProcess(poll_error=RuntimeError("poll failed"))]
    config = _config(tmp_path)
    config.auto_launch = True
    config.max_parallel_logins = 1
    daemon = AuthHelperDaemon(config, client=client, notifier=_FakeNotifier(), launcher=launcher)

    daemon.run_once()
    daemon.run_once()

    assert [request.id for request in launcher.launched] == ["auth_001"]
    assert set(daemon._active) == {"auth_001"}


def test_auto_launch_disabled_does_not_consume_child_process_capacity(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request("auth_001"), _request("auth_002")])
    config = _config(tmp_path)
    config.max_parallel_logins = 1
    daemon = AuthHelperDaemon(config, client=client, notifier=_FakeNotifier(), launcher=_FakeLauncher())

    daemon.run_once()

    assert [request_id for request_id, kind in client.events if kind == "launch_requested"] == ["auth_001", "auth_002"]


def test_failed_launch_does_not_leave_stale_capacity(tmp_path: Path) -> None:
    client = _FakeHelperClient(pending=[_request("auth_001"), _request("auth_002")])
    launcher = _FailingLauncher()
    config = _config(tmp_path)
    config.auto_launch = True
    config.max_parallel_logins = 1
    daemon = AuthHelperDaemon(config, client=client, notifier=_FakeNotifier(), launcher=launcher)

    daemon.run_once()

    assert [request.id for request in launcher.launched] == ["auth_001", "auth_002"]
    assert daemon._active == {}
    assert [kind for _request_id, kind in client.events if kind == "login_failed"] == ["login_failed", "login_failed"]


def test_notifier_format_is_readable() -> None:
    message = DesktopNotifier._format(
        AuthHelperRequest("auth_001", "proj_001", "target-user", None, "pending")
    )
    assert "Cairn requires authentication" in message
    assert "Project: proj_001" in message
    assert "Target: target-user" in message


def test_notifier_does_not_render_unavailable_narrow_fields() -> None:
    message = DesktopNotifier._format(
        AuthHelperRequest("auth_001", "proj_001", "target-user", None, "pending")
    )
    assert "Role:" not in message
    assert "Reason:" not in message
    assert "None" not in message


def test_helper_event_client_rejects_untrusted_event_shapes() -> None:
    class _Transport:
        def create_auth_event(self, _body):
            return ApiResult(status_code=201)

    client = AuthHelperClient(_Transport())
    request = AuthHelperRequest("auth_001", "proj_001", "target-user", None, "pending")
    with pytest.raises(ValueError, match="unsupported auth event"):
        client.submit_event(request, "arbitrary", capture_generation=1)
    with pytest.raises(ValueError, match="capture_generation"):
        client.submit_event(request, "browser_opened", capture_generation=1)


def test_helper_event_keys_are_stable_for_request_retries() -> None:
    class _Transport:
        def __init__(self) -> None:
            self.bodies: list[dict] = []

        def create_auth_event(self, body):
            self.bodies.append(body)
            return ApiResult(status_code=201)

    transport = _Transport()
    client = AuthHelperClient(transport)
    request = AuthHelperRequest("auth_001", "proj_001", "target-user", None, "pending")
    assert client.submit_event(request, "launch_requested", occurred_at="2026-01-01T00:00:00Z")
    assert client.submit_event(request, "launch_requested", occurred_at="2026-02-01T00:00:00Z")
    assert transport.bodies[0]["idempotency_key"] == transport.bodies[1]["idempotency_key"]
    assert transport.bodies[0]["occurred_at"] == transport.bodies[1]["occurred_at"]


def test_helper_pending_discovery_uses_only_configured_project_scope() -> None:
    class _Transport:
        def list_auth_helper_pending(self, project_id: str):
            assert project_id == "proj_001"
            return ApiResult(status_code=200, data=[])

        def _request_json(self, *_args, **_kwargs):
            raise AssertionError("global project discovery is forbidden")

    assert AuthHelperClient(_Transport()).list_pending("proj_001") == []


def test_helper_claim_poll_requires_exact_bound_actor() -> None:
    class _Transport:
        def get_auth_helper_view(self, _project_id: str, _request_id: str):
            return ApiResult(
                status_code=200,
                data={"id": "auth_001", "auth_ref": "target-user", "status": "claimed", "helper_actor_id": "helper-b"},
            )

    request = AuthHelperRequest("auth_001", "proj_001", "target-user", None, "pending")
    assert AuthHelperClient(_Transport()).wait_until_claimed(request, actor_id="helper-a", timeout_seconds=0) is False


def test_helper_verify_poll_accepts_waiting_user_only_for_bound_actor() -> None:
    class _Transport:
        def __init__(self, actor_id: str) -> None:
            self.actor_id = actor_id

        def get_auth_helper_view(self, _project_id: str, _request_id: str):
            return ApiResult(
                status_code=200,
                data={
                    "id": "auth_001",
                    "auth_ref": "target-user",
                    "status": "waiting_user",
                    "helper_actor_id": self.actor_id,
                },
            )

    request = AuthHelperRequest("auth_001", "proj_001", "target-user", None, "waiting_user")
    assert AuthHelperClient(_Transport("helper-a")).wait_until_verifiable(
        request, actor_id="helper-a", timeout_seconds=0
    ) is True
    assert AuthHelperClient(_Transport("helper-b")).wait_until_verifiable(
        request, actor_id="helper-a", timeout_seconds=0
    ) is False


def test_generic_protocol_client_exposes_no_direct_auth_transitions() -> None:
    from cairn.dispatcher.protocol.client import CairnClient

    for name in ("claim_auth_request", "auth_request_waiting", "auth_request_verifying", "auth_request_complete", "auth_request_fail"):
        assert not hasattr(CairnClient, name)


def test_helper_acknowledges_launch_event_before_starting_browser(tmp_path: Path) -> None:
    client = _EventHelperClient(pending=[_request()])
    launcher = _OrderingLauncher(client.events)
    config = _config(tmp_path)
    config.auto_launch = True
    daemon = AuthHelperDaemon(config, client=client, notifier=_FakeNotifier(), launcher=launcher)

    daemon.run_once()

    assert client.events == [("auth_001", "launch_requested"), ("auth_001", "browser_opened")]
    assert launcher.events == [("auth_001", "launch_requested"), ("auth_001", "browser_opened")]


def test_helper_waits_for_dispatcher_claim_before_browser_launch(tmp_path: Path) -> None:
    client = _ClaimPollingClient(pending=[_request()], claimed=False)
    launcher = _FakeLauncher()
    config = _config(tmp_path)
    config.auto_launch = True
    daemon = AuthHelperDaemon(config, client=client, notifier=_FakeNotifier(), launcher=launcher)

    daemon.run_once()

    assert client.events == [("auth_001", "launch_requested")]
    assert client.wait_calls == 1
    assert launcher.launched == []


class _EventHelperClient:
    def __init__(self, pending: list[AuthRequest]) -> None:
        self.pending = pending
        self.events: list[tuple[str, str]] = []

    def list_pending(self, project_id: str | None = None) -> list[AuthRequest]:
        return list(self.pending)

    def submit_event(self, request: AuthRequest, kind: str, **_kwargs: object) -> bool:
        self.events.append((request.id, kind))
        return True

    def wait_until_claimed(self, request: AuthRequest, **_kwargs: object) -> bool:
        return True


class _ClaimPollingClient(_EventHelperClient):
    def __init__(self, pending: list[AuthRequest], claimed: bool) -> None:
        super().__init__(pending)
        self.claimed = claimed
        self.wait_calls = 0

    def wait_until_claimed(self, request: AuthRequest, **_kwargs: object) -> bool:
        self.wait_calls += 1
        return self.claimed


class _OrderingLauncher(_FakeLauncher):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__()
        self.events = events

    def launch(self, request: AuthRequest):
        assert self.events == [(request.id, "launch_requested")]
        return super().launch(request)
