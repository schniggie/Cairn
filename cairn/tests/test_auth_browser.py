from __future__ import annotations

import sys
import types
from pathlib import Path

from click.testing import CliRunner

from cairn.auth.manager import AuthManager
from cairn.auth.models import AuthVerificationResult
from cairn.auth.verifier import AuthVerifier
from cairn.dispatcher.config import AuthTargetConfig


class PlaywrightError(Exception):
    """Stand-in for playwright.sync_api.Error so tests do not require the package."""


def _target(*, indexed_db: bool = True) -> AuthTargetConfig:
    return AuthTargetConfig.model_validate(
        {
            "name": "target",
            "base_url": "https://example.test",
            "login_url": "https://example.test/login",
            "role": "user",
            "indexed_db": indexed_db,
            "verify": {"url": "https://example.test/home"},
        }
    )


class _FakeContext:
    def __init__(self, verification: AuthVerificationResult | None = None):
        self.verification = verification
        self.storage_state_calls: list[dict] = []

    def new_page(self):
        return types.SimpleNamespace()

    def storage_state(self, **kwargs):
        self.storage_state_calls.append(kwargs)
        return {"cookies": []}

    def close(self):
        pass


def test_capture_passes_indexed_db_to_storage_state(monkeypatch) -> None:
    context = _FakeContext()
    page = types.SimpleNamespace(
        goto=lambda *args, **kwargs: types.SimpleNamespace(status=200),
        wait_for_selector=lambda *args, **kwargs: None,
        close=lambda: None,
    )
    context.new_page = lambda: page
    browser = types.SimpleNamespace(
        new_context=lambda: context,
        close=lambda: None,
    )
    playwright = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(Error=PlaywrightError, sync_playwright=lambda: _Playwright(playwright)))
    manager = AuthManager()
    manager._wait_for_verified_session = lambda *args, **kwargs: AuthVerificationResult(True, True, True, True)

    result = manager.capture_interactive(_target(indexed_db=False), login_timeout_seconds=1)

    assert result.storage_state == {"cookies": []}
    assert context.storage_state_calls == [{"indexed_db": False}]


def test_verify_storage_state_returns_invalid_on_playwright_error(monkeypatch) -> None:
    class _BrokenPlaywright:
        def __enter__(self):
            raise PlaywrightError("browser failed")

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(Error=PlaywrightError, sync_playwright=lambda: _BrokenPlaywright()))

    result = AuthVerifier().verify_storage_state(_target(), {"cookies": []})

    assert result == AuthVerificationResult(False, False, False, False, "browser failed")


def test_capture_returns_invalid_on_authoritative_playwright_error(monkeypatch) -> None:
    context = _FakeContext()
    page = types.SimpleNamespace(
        goto=lambda *args, **kwargs: types.SimpleNamespace(status=200),
        wait_for_selector=lambda *args, **kwargs: None,
        close=lambda: None,
    )
    context.new_page = lambda: page
    browser = types.SimpleNamespace(new_context=lambda: context, close=lambda: None)
    playwright = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(Error=PlaywrightError, sync_playwright=lambda: _Playwright(playwright)))

    monkeypatch.setattr(
        AuthVerifier,
        "_verify_context",
        lambda *args: (_ for _ in ()).throw(PlaywrightError("network failed")),
    )
    manager = AuthManager()

    result = manager.capture_interactive(_target(), login_timeout_seconds=1)

    assert result.verification == AuthVerificationResult(False, False, False, False, "network failed")


def test_login_without_request_is_local_only(monkeypatch, tmp_path: Path) -> None:
    from cairn.auth.models import AuthCaptureResult
    from cairn.cli import main

    target = _target()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text("{}", encoding="utf-8")
    config = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            store_root=str(tmp_path),
            login_timeout=1,
            verify_timeout=1,
            helper_token_env="CAIRN_AUTH_HELPER_TOKEN",
            helper_actor_id="helper-a",
            target=lambda _name: target,
        ),
        server="http://unused",
    )
    monkeypatch.setattr("cairn.dispatcher.config.DispatchConfig.load", lambda _path: config)
    monkeypatch.setattr(
        "cairn.auth.manager.AuthManager.capture_interactive",
        lambda self, _target, **_kwargs: AuthCaptureResult(
            storage_state={"cookies": []},
            verification=AuthVerificationResult(True, True, True, True),
        ),
    )
    monkeypatch.setattr(
        "cairn.auth.verifier.AuthVerifier.verify_storage_state",
        lambda self, _target, _state: AuthVerificationResult(True, True, True, True),
    )
    monkeypatch.setattr(
        "cairn.dispatcher.protocol.client.CairnClient.__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local login opened a protocol client")),
    )

    result = CliRunner().invoke(
        main,
        ["auth", "login", "--config", str(config_path), "--project", "p1", "--target", "target"],
    )

    assert result.exit_code == 0, result.output
    assert "AuthSessionVerified fact recorded" not in result.output


def test_login_with_request_emits_browser_opened_before_capture_and_success(monkeypatch, tmp_path: Path) -> None:
    from cairn.auth.models import AuthCaptureResult
    from cairn.cli import main

    target = _target()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text("{}", encoding="utf-8")
    config = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            store_root=str(tmp_path),
            login_timeout=1,
            verify_timeout=1,
            helper_token_env="CAIRN_AUTH_HELPER_TOKEN",
            helper_actor_id="helper-a",
            target=lambda _name: target,
        ),
        server="http://unused",
    )
    events: list[tuple[str, str, int | None]] = []

    class _Events:
        client = types.SimpleNamespace(close=lambda: None)

        def wait_until_claimed(self, *_args, **_kwargs):
            return True

        def wait_until_claimed_fields(self, *_args, **_kwargs):
            return True

        def submit_event_fields(self, project_id, request_id, auth_ref, kind, *, capture_generation=None):
            events.append((request_id, kind, capture_generation))
            return True

    monkeypatch.setattr("cairn.dispatcher.config.DispatchConfig.load", lambda _path: config)
    monkeypatch.setattr("cairn.auth_helper.client.AuthHelperClient", lambda _client: _Events())
    monkeypatch.setattr(
        "cairn.auth.manager.AuthManager.capture_interactive",
        lambda self, _target, **_kwargs: AuthCaptureResult(
            storage_state={"cookies": []},
            verification=AuthVerificationResult(True, True, True, True),
        ),
    )
    monkeypatch.setattr(
        "cairn.auth.verifier.AuthVerifier.verify_storage_state",
        lambda self, _target, _state: AuthVerificationResult(True, True, True, True),
    )

    result = CliRunner().invoke(
        main,
        [
            "auth", "login", "--config", str(config_path), "--project", "p1",
            "--target", "target", "--request", "auth-001",
        ],
    )

    assert result.exit_code == 0, result.output
    assert events == [
        ("auth-001", "browser_opened", None),
        ("auth-001", "login_succeeded", 1),
    ]
    assert "fact recorded" not in result.output


def test_request_bound_login_does_not_open_browser_before_dispatcher_claim(monkeypatch, tmp_path: Path) -> None:
    from cairn.auth.models import AuthCaptureResult
    from cairn.cli import main

    target = _target()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text("{}", encoding="utf-8")
    config = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            store_root=str(tmp_path), login_timeout=1, verify_timeout=1,
            helper_token_env="CAIRN_AUTH_HELPER_TOKEN", helper_actor_id="helper-a",
            target=lambda _name: target,
        ), server="http://unused",
    )
    called = {"capture": False}
    class _Events:
        client = types.SimpleNamespace(close=lambda: None)
        def wait_until_claimed(self, *_args, **_kwargs):
            return False
    monkeypatch.setattr("cairn.dispatcher.config.DispatchConfig.load", lambda _path: config)
    monkeypatch.setattr("cairn.auth_helper.client.AuthHelperClient", lambda _client: _Events())
    monkeypatch.setattr(
        "cairn.auth.manager.AuthManager.capture_interactive",
        lambda self, _target, **_kwargs: called.__setitem__("capture", True) or AuthCaptureResult(None, AuthVerificationResult(False, False, False, False)),
    )
    result = CliRunner().invoke(main, ["auth", "login", "--config", str(config_path), "--project", "p1", "--target", "target", "--request", "auth-001"])
    assert result.exit_code != 0
    assert called["capture"] is False


def test_request_bound_login_rejecting_browser_opened_does_not_capture(monkeypatch, tmp_path: Path) -> None:
    from cairn.auth.models import AuthCaptureResult
    from cairn.cli import main

    target = _target()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text("{}", encoding="utf-8")
    config = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            store_root=str(tmp_path), login_timeout=1, verify_timeout=1,
            helper_token_env="CAIRN_AUTH_HELPER_TOKEN", helper_actor_id="helper-a",
            target=lambda _name: target,
        ), server="http://unused",
    )
    called = {"capture": False}

    class _Events:
        client = types.SimpleNamespace(close=lambda: None)

        def wait_until_claimed_fields(self, *_args, **_kwargs):
            return True

        def submit_event_fields(self, _project, _request, _auth_ref, kind, **_kwargs):
            return kind != "browser_opened"

    monkeypatch.setattr("cairn.dispatcher.config.DispatchConfig.load", lambda _path: config)
    monkeypatch.setattr("cairn.auth_helper.client.AuthHelperClient", lambda _client: _Events())
    monkeypatch.setattr(
        "cairn.auth.manager.AuthManager.capture_interactive",
        lambda self, _target, **_kwargs: called.__setitem__("capture", True)
        or AuthCaptureResult({"cookies": []}, AuthVerificationResult(True, True, True, True)),
    )

    result = CliRunner().invoke(
        main,
        ["auth", "login", "--config", str(config_path), "--project", "p1",
         "--target", "target", "--request", "auth-001"],
    )

    assert result.exit_code != 0
    assert called["capture"] is False


def test_request_bound_login_fails_fast_in_legacy_control_plane(monkeypatch, tmp_path: Path) -> None:
    from cairn.cli import main

    target = _target()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text("{}", encoding="utf-8")
    config = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            store_root=str(tmp_path), login_timeout=1, verify_timeout=1,
            helper_token_env="CAIRN_AUTH_HELPER_TOKEN", helper_actor_id="helper-a",
            target=lambda _name: target,
        ), server="http://unused", auth_control_plane_mode="legacy",
    )
    monkeypatch.setattr("cairn.dispatcher.config.DispatchConfig.load", lambda _path: config)

    result = CliRunner().invoke(
        main,
        ["auth", "login", "--config", str(config_path), "--project", "p1",
         "--target", "target", "--request", "auth-001"],
    )

    assert result.exit_code != 0
    assert "dual_write" in result.output


def test_request_bound_verify_validates_capture_before_submitting_event(monkeypatch, tmp_path: Path) -> None:
    from cairn.auth.store import AuthStore
    from cairn.cli import main

    target = _target()
    store = AuthStore(tmp_path)
    store.write_capture("p1", "target", {"cookies": []}, request_id="auth-001", actor_id="helper-a")
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text("{}", encoding="utf-8")
    config = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            store_root=str(tmp_path), login_timeout=1, verify_timeout=1,
            helper_token_env="CAIRN_AUTH_HELPER_TOKEN", helper_actor_id="helper-a",
            target=lambda _name: target,
        ), server="http://unused",
    )
    class _Events:
        client = types.SimpleNamespace(close=lambda: None)
        def wait_until_verifiable_fields(self, *_args, **_kwargs): return True
        def submit_event_fields(self, *_args, **_kwargs): raise AssertionError("event emitted for invalid capture")
    monkeypatch.setattr("cairn.dispatcher.config.DispatchConfig.load", lambda _path: config)
    monkeypatch.setattr("cairn.auth_helper.client.AuthHelperClient", lambda _client: _Events())
    state = store.state_file("p1", "target")
    state.write_text('{"cookies":[{"name":"tampered"}]}', encoding="utf-8")
    result = CliRunner().invoke(main, ["auth", "verify", "--config", str(config_path), "--project", "p1", "--target", "target", "--request", "auth-001"])
    assert result.exit_code != 0


def test_request_bound_verify_accepts_waiting_user_after_browser_opened(monkeypatch, tmp_path: Path) -> None:
    from cairn.auth.models import AuthMeta, utcnow
    from cairn.auth.store import AuthStore
    from cairn.cli import main

    target = _target()
    store = AuthStore(tmp_path)
    manifest = store.write_capture(
        "p1", "target", {"cookies": []}, request_id="auth-001", actor_id="helper-a"
    )
    store.write_meta(
        "p1", "target", AuthMeta(
            target="target", role=target.role, base_url=target.base_url, created_at=utcnow()
        )
    )
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text("{}", encoding="utf-8")
    config = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            store_root=str(tmp_path), login_timeout=1, verify_timeout=1,
            helper_token_env="CAIRN_AUTH_HELPER_TOKEN", helper_actor_id="helper-a",
            target=lambda _name: target,
        ), server="http://unused",
    )
    events: list[tuple[str, int | None]] = []

    class _Events:
        client = types.SimpleNamespace(close=lambda: None)

        def wait_until_verifiable_fields(self, *_args, **_kwargs):
            return True

        def submit_event_fields(self, _project, _request, _auth_ref, kind, **kwargs):
            assert kind == "login_succeeded"
            events.append((kind, kwargs.get("capture_generation")))
            return True

    monkeypatch.setattr("cairn.dispatcher.config.DispatchConfig.load", lambda _path: config)
    monkeypatch.setattr("cairn.auth_helper.client.AuthHelperClient", lambda _client: _Events())
    monkeypatch.setattr(
        "cairn.auth.verifier.AuthVerifier.verify_storage_state",
        lambda self, _target, _state: AuthVerificationResult(True, True, True, True),
    )

    result = CliRunner().invoke(
        main,
        ["auth", "verify", "--config", str(config_path), "--project", "p1",
         "--target", "target", "--request", "auth-001"],
    )

    assert result.exit_code == 0, result.output
    assert events == [("login_succeeded", manifest.capture_generation)]


class _Playwright:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False
