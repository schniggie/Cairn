from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.harness import HarnessResult, resolve_target_credential
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.tasks import verify
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import Fact, Intent
from conftest import FakeContainerManager, make_config, make_project


def _binding(**overrides):
    data = {
        "id": "demo",
        "env": "CAIRN_SECRET_DEMO",
        "base_url": "http://127.0.0.1:18080",
        "allowlist": ["127.0.0.1:18080"],
    }
    data.update(overrides)
    return data


def _config(*, credentials: list[dict] | None = None, require_fire_approval: bool = True) -> DispatchConfig:
    payload = make_config().model_dump()
    payload["tasks"]["verify"]["max_rounds"] = 1
    payload["tasks"]["verify"]["require_fire_approval"] = require_fire_approval
    if credentials is not None:
        payload["target_credentials"] = credentials
    return DispatchConfig.model_validate(payload)


def _project(base_url: str, credentials_ref: str | None, allowlist: list[str] | None = None):
    project = make_project()
    target: dict[str, str] = {"base_url": base_url}
    if credentials_ref is not None:
        target["credentials_ref"] = credentials_ref
    project.facts[0] = Fact(
        id="origin",
        description=json.dumps(
            {
                "target": target,
                "allowlist": allowlist or ["evil.example", "127.0.0.1:9"],
            }
        ),
    )
    return project


def _intent(fire_status: str = "approved") -> Intent:
    return Intent(
        id="i1",
        from_=["origin"],
        description="VERIFY",
        creator="t",
        created_at="2026-01-01T00:00:00Z",
        task_kind="verify",
        fire_status=fire_status,  # type: ignore[arg-type]
        poc_brief={
            "entry": {"endpoint": "POST /api/import"},
            "chain": ["origin"],
            "payload_recipe": {"shape": "body"},
            "success_signature": {"kind": "response_match", "check": "OK"},
        },
    )


class _Lease:
    failure = None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def attach_process(self, _process) -> None:
        return None


class _Client:
    def get_verify_control(self, _project_id: str) -> dict:
        return {"kill_requested": False}

    def record_proxy_traffic(self, *_args, **_kwargs):
        return None

    def conclude_observations(self, *_args, **_kwargs) -> ApiResult:
        return ApiResult(200, {})

    def release(self, *_args, **_kwargs) -> ApiResult:
        return ApiResult(200, {})


def _run(monkeypatch, config, project, intent, capture: list | None = None):
    monkeypatch.setattr(verify.HeartbeatLease, "for_intent", lambda *_a, **_k: _Lease())

    def _exec(**kwargs):
        if capture is not None:
            capture.append(kwargs)
        return HarnessResult(
            triggered=False,
            why_failed={"reason": "no_signal", "detail": "stop"},
            request="r",
            response="s",
        )

    monkeypatch.setattr(verify, "execute_allowed_request", _exec)
    containers = FakeContainerManager()
    outcome = verify.run_verify_task(
        config,
        _Client(),  # type: ignore[arg-type]
        containers,
        project,
        "",
        intent,
        config.workers[0],
        TaskCancellation(),
    )
    return outcome, containers


def test_operator_binding_supplies_env_name_and_destination(monkeypatch):
    monkeypatch.setenv("CAIRN_SECRET_demo", "from-origin-name")
    monkeypatch.setenv("CAIRN_SECRET_DEMO", "operator-secret")
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "safety")
    monkeypatch.setenv("CAIRN_ADMIN_TOKEN", "admin")
    resolved = resolve_target_credential(
        "secret:demo",
        [SimpleNamespace(**_binding())],
        origin_base_url="http://127.0.0.1:18080/",
    )
    assert resolved.attached is True
    assert resolved.env["CAIRN_TARGET_CREDENTIAL"] == "operator-secret"
    assert resolved.base_url == "http://127.0.0.1:18080"
    assert resolved.allowlist == ("127.0.0.1:18080",)
    assert "from-origin-name" not in resolved.env.values()
    assert "safety" not in resolved.env.values()
    assert "admin" not in resolved.env.values()


def test_env_scheme_and_internal_names_are_not_resolved(monkeypatch):
    monkeypatch.setenv("CAIRN_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "safety")
    monkeypatch.setenv("CAIRN_AUTH_HELPER_TOKEN", "helper")
    binding = SimpleNamespace(**_binding())
    for ref in ("env:CAIRN_ADMIN_TOKEN", "env:CAIRN_SAFETY_TOKEN", "secret:CAIRN_SAFETY_TOKEN"):
        resolved = resolve_target_credential(ref, [binding], origin_base_url="http://127.0.0.1:18080")
        assert resolved.attached is False
        assert resolved.env == {}
        assert "admin" not in resolved.env.values()
        assert "safety" not in resolved.env.values()
        assert "helper" not in resolved.env.values()

    internal = SimpleNamespace(**_binding(env="CAIRN_SAFETY_TOKEN"))
    denied = resolve_target_credential("secret:demo", [internal], origin_base_url="http://127.0.0.1:18080")
    assert denied.error == "target credential env is a dispatcher-internal secret"
    assert denied.env == {}
    assert "safety" not in denied.env.values()


def test_origin_destination_mismatch_refuses_credential(monkeypatch):
    monkeypatch.setenv("CAIRN_SECRET_DEMO", "operator-secret")
    resolved = resolve_target_credential(
        "demo",
        [SimpleNamespace(**_binding())],
        origin_base_url="http://203.0.113.9:9",
    )
    assert resolved.requested is True
    assert resolved.attached is False
    assert resolved.error == "origin target does not match the credential destination"
    assert resolved.env == {}


def test_config_rejects_internal_and_duplicate_credentials():
    with pytest.raises(ValidationError, match="dispatcher-internal"):
        _config(credentials=[_binding(env="CAIRN_ADMIN_TOKEN")])
    with pytest.raises(ValidationError, match="dispatcher-internal"):
        _config(credentials=[_binding(env="cairn_safety_token")])
    payload = make_config().model_dump()
    payload["safety"]["token_env"] = "MY_SAFETY"
    payload["target_credentials"] = [_binding(env="MY_SAFETY")]
    with pytest.raises(ValidationError, match="dispatcher-internal"):
        DispatchConfig.model_validate(payload)
    with pytest.raises(ValidationError, match="unique"):
        _config(credentials=[_binding(), _binding(base_url="http://127.0.0.1:9")])


def test_verify_uses_operator_destination_not_origin_allowlist(monkeypatch):
    monkeypatch.setenv("CAIRN_SECRET_DEMO", "operator-secret")
    capture: list[dict] = []
    outcome, containers = _run(
        monkeypatch,
        _config(credentials=[_binding()]),
        _project("http://127.0.0.1:18080", "secret:demo", ["evil.example"]),
        _intent(),
        capture,
    )
    assert outcome == "success"
    assert containers.ensure_calls[0]["extra_env"]["CAIRN_TARGET_CREDENTIAL"] == "operator-secret"
    assert capture[0]["base_url"] == "http://127.0.0.1:18080"
    assert capture[0]["allowlist"] == ["127.0.0.1:18080"]
    assert "evil.example" not in capture[0]["allowlist"]
    assert capture[0]["headers"]["Authorization"] == "Bearer operator-secret"


def test_verify_refuses_mismatched_origin_without_sending(monkeypatch):
    monkeypatch.setenv("CAIRN_SECRET_DEMO", "operator-secret")
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "safety")
    capture: list[dict] = []
    outcome, containers = _run(
        monkeypatch,
        _config(credentials=[_binding()]),
        _project("http://203.0.113.9", "secret:demo"),
        _intent(),
        capture,
    )
    assert outcome == "failed"
    assert capture == []
    assert containers.ensure_calls == []
    stolen, stolen_containers = _run(
        monkeypatch,
        _config(credentials=[_binding()]),
        _project("http://203.0.113.9", "secret:CAIRN_SAFETY_TOKEN"),
        _intent(),
        capture,
    )
    assert stolen == "failed"
    assert capture == []
    assert stolen_containers.ensure_calls == []


def test_credentialed_verify_still_requires_approval_when_gate_disabled(monkeypatch):
    capture: list[dict] = []
    outcome, containers = _run(
        monkeypatch,
        _config(credentials=[_binding()], require_fire_approval=False),
        _project("http://127.0.0.1:18080", "secret:demo"),
        _intent("pending"),
        capture,
    )
    assert outcome == "awaiting_approval"
    assert capture == []
    assert containers.ensure_calls == []
    assert verify.verify_may_run(False, "pending", True) is False
    assert verify.verify_may_run(False, "approved", True) is True
    assert verify.verify_may_run(False, None, False) is True


def test_verify_fire_fails_closed_without_admin_token(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr("cairn.server.app.ADMIN_TOKEN", "")
    db.configure(tmp_path / "fire.db")
    with TestClient(app) as client:
        created = client.post("/projects", json={"title": "open", "origin": "notes", "goal": "look"})
        assert created.status_code == 201
        pid = created.json()["project"]["id"]
        intent = client.post(
            f"/projects/{pid}/intents",
            json={"from": ["origin"], "description": "look", "creator": "t"},
        )
        assert intent.status_code == 201
        fired = client.post(
            f"/projects/{pid}/intents/{intent.json()['id']}/fire",
            json={"action": "approve", "actor": "human"},
            headers={"Authorization": "Bearer anything"},
        )
        assert fired.status_code == 403


def test_verify_fire_requires_configured_admin_bearer(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr("cairn.server.app.ADMIN_TOKEN", "test-admin")
    db.configure(tmp_path / "fire-auth.db")
    with TestClient(app) as client:
        created = client.post(
            "/projects",
            json={"title": "gated", "origin": "notes", "goal": "look"},
            headers={"Authorization": "Bearer test-admin"},
        )
        assert created.status_code == 201
        pid = created.json()["project"]["id"]
        intent = client.post(
            f"/projects/{pid}/intents",
            json={"from": ["origin"], "description": "look", "creator": "t"},
            headers={"Authorization": "Bearer test-admin"},
        )
        intent_id = intent.json()["id"]
        denied = client.post(
            f"/projects/{pid}/intents/{intent_id}/fire",
            json={"action": "approve", "actor": "human"},
        )
        assert denied.status_code == 403
        approved = client.post(
            f"/projects/{pid}/intents/{intent_id}/fire",
            json={"action": "approve", "actor": "human"},
            headers={"Authorization": "Bearer test-admin"},
        )
        assert approved.status_code == 200
        assert approved.json()["fire_status"] == "approved"
