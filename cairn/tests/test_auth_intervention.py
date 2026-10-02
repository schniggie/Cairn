from __future__ import annotations

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import reason
from cairn.dispatcher.workers.health import HealthResult

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_intent,
    make_project,
)


def _lease_factory(lease):
    def _factory(*_a, **_k):
        return lease

    return _factory


class _InterventionClient(FakeClient):
    """FakeClient extended with auth-request recording."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created_auth_requests: list[tuple] = []

    def create_auth_request(
        self,
        project_id,
        source_fact_ids,
        auth_ref,
        role,
        reason,
        login_url=None,
    ):
        self.created_auth_requests.append(
            (project_id, source_fact_ids, auth_ref, role, reason, login_url)
        )
        return ApiResult(201, {"id": "auth_001"})

    def create_auth_request_internal(
        self,
        project_id,
        source_fact_ids,
        auth_ref,
    ):
        self.created_auth_requests.append(
            (project_id, source_fact_ids, auth_ref)
        )
        return ApiResult(201, {"id": "auth_001"})


def test_reason_creates_auth_request_from_intervention(monkeypatch) -> None:
    config = _config_with_auth([])
    project = make_project()
    client = _InterventionClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],"target":"target-user","role":"user","reason":"orders need auth"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == []
    assert client.created_auth_requests == [
        ("proj_001", ["f001"], "target-user", "user", "orders need auth", None)
    ]


def test_reason_internal_create_uses_configured_target_authority(monkeypatch) -> None:
    config = _config_with_auth([])
    config = config.model_copy(update={"auth_control_plane_mode": "dual_write"})
    project = make_project()
    client = _InterventionClient(project)
    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(FakeLease()))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],"target":"target-user","role":"forged","reason":"secret llm reason","login_url":"https://evil.invalid"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_auth_requests == [
        ("proj_001", ["f001"], "target-user")
    ]


def test_reason_creates_intents_and_interventions_together(monkeypatch) -> None:
    config = _config_with_auth([], target_name="target-admin", target_role="admin")
    project = make_project()
    client = _InterventionClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"public api"}],'
            '"interventions":[{"type":"auth","from":["f002"],"target":"target-admin","role":"admin","reason":"admin area"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "public api", "test-worker")]
    assert client.created_auth_requests == [
        ("proj_001", ["f002"], "target-admin", "admin", "admin area", None)
    ]


def test_reason_noop_returns_success(monkeypatch) -> None:
    config = make_config()
    # Provide an open intent so noop is legal (no new proposals needed).
    project = make_project(intents=[make_intent()])
    client = _InterventionClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == []
    assert client.created_auth_requests == []


def _config_with_auth(
    allow_roles: list[str] | None,
    target_role: str = "user",
    target_name: str = "target-user",
) -> DispatchConfig:
    base = make_config().model_dump()
    base["auth"] = {
        "store_root": "/opt/cairn/auth",
        "intervention": {"allow_roles": allow_roles or []},
        "targets": [
            {
                "name": target_name,
                "base_url": "https://t.example.com",
                "login_url": "https://t.example.com/login",
                "role": target_role,
                "verify": {"url": "https://t.example.com/dashboard"},
            }
        ],
    }
    return DispatchConfig.model_validate(base)


def _run_reason_with_intervention(monkeypatch, config, project, client, intervention_json: str) -> str:
    containers = FakeContainerManager()
    lease = FakeLease()
    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(0, intervention_json, ""),
    )
    return reason.run_reason_task(
        config, client, containers, project, "graph", config.workers[0], TaskCancellation()
    )


def test_allow_roles_empty_allows_all(monkeypatch) -> None:
    config = _config_with_auth(allow_roles=[])
    project = make_project()
    client = _InterventionClient(project)

    outcome = _run_reason_with_intervention(
        monkeypatch,
        config,
        project,
        client,
        '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],'
        '"target":"target-user","role":"user","reason":"need auth"}]}}',
    )

    assert outcome == "success"
    assert len(client.created_auth_requests) == 1


def test_allow_roles_blocks_disallowed_role(monkeypatch) -> None:
    # Config declares target role "user", allow_roles only permits "admin".
    config = _config_with_auth(allow_roles=["admin"], target_role="user")
    project = make_project()
    client = _InterventionClient(project)

    # Even if the LLM forges "admin", the authoritative role is config's "user".
    outcome = _run_reason_with_intervention(
        monkeypatch,
        config,
        project,
        client,
        '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],'
        '"target":"target-user","role":"admin","reason":"need auth"}]}}',
    )

    assert outcome == "success"  # no error, but the request was blocked
    assert client.created_auth_requests == []


def test_allow_roles_allows_permitted_role(monkeypatch) -> None:
    config = _config_with_auth(allow_roles=["admin"], target_role="admin")
    project = make_project()
    client = _InterventionClient(project)

    outcome = _run_reason_with_intervention(
        monkeypatch,
        config,
        project,
        client,
        '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],'
        '"target":"target-user","role":"admin","reason":"need auth"}]}}',
    )

    assert outcome == "success"
    assert len(client.created_auth_requests) == 1
    # auth_ref is the config target name; role is the authoritative config role.
    assert client.created_auth_requests[0][2] == "target-user"
    assert client.created_auth_requests[0][3] == "admin"


def test_unknown_target_skipped(monkeypatch) -> None:
    config = _config_with_auth(allow_roles=[], target_role="user")
    project = make_project()
    client = _InterventionClient(project)

    outcome = _run_reason_with_intervention(
        monkeypatch,
        config,
        project,
        client,
        '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],'
        '"target":"does-not-exist","role":"user","reason":"need auth"}]}}',
    )

    assert outcome == "success"
    assert client.created_auth_requests == []


def test_intervention_is_skipped_when_auth_config_is_missing(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = _InterventionClient(project)

    outcome = _run_reason_with_intervention(
        monkeypatch,
        config,
        project,
        client,
        '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],'
        '"target":"target-user","role":"user","reason":"need auth"}]}}',
    )

    assert outcome == "success"
    assert client.created_auth_requests == []


def test_intervention_is_skipped_when_auth_interventions_are_disabled(monkeypatch) -> None:
    config = _config_with_auth([])
    config.auth.intervention.enabled = False
    project = make_project()
    client = _InterventionClient(project)

    outcome = _run_reason_with_intervention(
        monkeypatch,
        config,
        project,
        client,
        '{"accepted":true,"data":{"interventions":[{"type":"auth","from":["f001"],'
        '"target":"target-user","role":"user","reason":"need auth"}]}}',
    )

    assert outcome == "success"
    assert client.created_auth_requests == []


def test_blocked_intervention_does_not_block_normal_intent(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = _InterventionClient(project)

    outcome = _run_reason_with_intervention(
        monkeypatch,
        config,
        project,
        client,
        '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"public api"}],'
        '"interventions":[{"type":"auth","from":["f002"],"target":"target-user",'
        '"role":"user","reason":"need auth"}]}}',
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "public api", "test-worker")]
    assert client.created_auth_requests == []
