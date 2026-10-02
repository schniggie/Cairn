from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server import dispatch_config_store
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_audit_response_exposes_ui_correlation_and_payload_fields(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "test-safety-token")
    project_id = _create_project(client)
    body = {
        "schema_version": 1,
        "event_id": "decision-1",
        "action_id": "action-1",
        "run_id": "run-1",
        "project_id": project_id,
        "intent_id": None,
        "worker": "pi-worker",
        "phase": "reason_execute",
        "event_type": "ACTION_DECISION",
        "tool_name": "bash",
        "decision": "block",
        "rule_id": "destructive_delete",
        "reason": "blocked before execution",
        "payload": {
            "proposal": {"tool_name": "bash", "input": {"command": "rm -rf /srv/data"}},
            "decision": {"target": "/srv/data"},
            "assistant": {"text": "[V1][BRANCH_CLOSED]"},
        },
    }
    created = client.post(
        "/internal/safety/events",
        json=body,
        headers={"X-Cairn-Safety-Token": "test-safety-token"},
    )
    assert created.status_code == 201

    item = client.get(f"/projects/{project_id}/audit").json()["items"][0]

    assert item["event_id"] == "decision-1"
    assert item["action_id"] == "action-1"
    assert item["run_id"] == "run-1"
    assert item["worker"] == "pi-worker"
    assert item["phase"] == "reason_execute"
    assert item["tool_name"] == "bash"
    assert item["decision"] == "block"
    assert item["payload"]["proposal"]["input"]["command"] == "rm -rf /srv/data"
    assert len(item["payload_sha256"]) == 64
    assert item["truncated"] is False


def test_project_workflow_create_conclude_complete_and_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    fact = response.json()["fact"]
    assert fact["id"] == "f001"
    assert fact["description"] == "new fact"

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["project"]["started_at"] is None
    assert payload["fact"]["id"] == "f002"
    assert payload["fact"]["description"] == "human correction"
    assert payload["intent"]["from"] == ["f001"]
    assert payload["intent"]["to"] == "f002"


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None
    assert response.json()["started_at"] is not None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403

    response = client.put(f"/projects/{project_id}/status", json={"status": "active"})
    assert response.status_code == 200
    assert response.json()["started_at"] is None


def test_intent_creation_rejects_goal_source_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["goal"], "description": "invalid", "creator": "reasoner", "worker": None},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert response.status_code == 200
    assert client.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "goal: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def test_record_intent_failure_increments_retry_and_degrades_to_stale_dead(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    intent_path = f"/projects/{project_id}/intents/i001"
    client.post(f"{intent_path}/heartbeat", json={"worker": "explorer"})

    response = client.post(f"{intent_path}/fail", json={"worker": "explorer"})
    assert response.status_code == 200
    assert response.json()["retry_count"] == 1
    assert response.json()["concluded_as"] is None

    for _ in range(2):
        response = client.post(f"{intent_path}/fail", json={"worker": "explorer"})
    assert response.json()["retry_count"] == 3
    assert response.json()["concluded_as"] == "stale"

    for _ in range(7):
        response = client.post(f"{intent_path}/fail", json={"worker": "explorer"})
    assert response.json()["retry_count"] == 10
    assert response.json()["concluded_as"] == "dead"


def test_record_intent_failure_does_not_require_worker_ownership(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    intent_path = f"/projects/{project_id}/intents/i001"
    client.post(f"{intent_path}/heartbeat", json={"worker": "worker-a"})

    response = client.post(f"{intent_path}/fail", json={"worker": "worker-b"})
    assert response.status_code == 200
    assert response.json()["retry_count"] == 1
    assert response.json()["worker"] == "worker-a"


def test_record_intent_failure_uses_custom_thresholds(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    intent_path = f"/projects/{project_id}/intents/i001"
    client.post(f"{intent_path}/heartbeat", json={"worker": "explorer"})

    body = {"worker": "explorer", "stale_retry_threshold": 2, "dead_retry_threshold": 4}
    client.post(f"{intent_path}/fail", json=body)
    response = client.post(f"{intent_path}/fail", json=body)
    assert response.json()["retry_count"] == 2
    assert response.json()["concluded_as"] == "stale"

    for _ in range(2):
        response = client.post(f"{intent_path}/fail", json=body)
    assert response.json()["retry_count"] == 4
    assert response.json()["concluded_as"] == "dead"


def test_record_intent_failure_rejects_concluded_intent(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    intent_path = f"/projects/{project_id}/intents/i001"
    client.post(f"{intent_path}/heartbeat", json={"worker": "explorer"})
    client.post(f"{intent_path}/conclude", json={"worker": "explorer", "description": "new fact"})

    response = client.post(f"{intent_path}/fail", json={"worker": "explorer"})
    assert response.status_code == 409


def test_conclude_sets_concluded_as_success(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    intent_path = f"/projects/{project_id}/intents/i001"
    client.post(f"{intent_path}/heartbeat", json={"worker": "explorer"})

    response = client.post(f"{intent_path}/conclude", json={"worker": "explorer", "description": "new fact"})
    assert response.status_code == 200
    assert response.json()["intent"]["concluded_as"] == "success"


def test_conclude_resets_retry_count(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    intent_path = f"/projects/{project_id}/intents/i001"
    client.post(f"{intent_path}/heartbeat", json={"worker": "explorer"})
    client.post(f"{intent_path}/fail", json={"worker": "explorer"})
    client.post(f"{intent_path}/fail", json={"worker": "explorer"})

    response = client.post(f"{intent_path}/conclude", json={"worker": "explorer", "description": "new fact"})
    assert response.status_code == 200
    assert response.json()["intent"]["retry_count"] == 0
    assert response.json()["intent"]["concluded_as"] == "success"


def test_runtime_events_are_persisted_and_incremental(client: TestClient) -> None:
    project_id = _create_project(client)

    initial = client.get(f"/projects/{project_id}/events").json()
    assert initial[0]["event_type"] == "project_created"

    created = client.post(
        f"/projects/{project_id}/events",
        json={
            "event_type": "task_started",
            "phase": "explore",
            "status": "running",
            "message": "worker started",
            "worker": "local-codex",
            "intent_id": "i001",
        },
    )
    assert created.status_code == 201
    event_id = created.json()["id"]
    incremental = client.get(f"/projects/{project_id}/events?after_id={event_id - 1}").json()
    assert [event["id"] for event in incremental] == [event_id]


def test_http_evidence_round_trip(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/http-records",
        json={
            "intent_id": "i001",
            "worker": "local-codex",
            "method": "post",
            "url": "https://target.test/login",
            "request": {"headers": {"content-type": "application/json"}, "body": "{\"admin\":true}"},
            "response": {"status": 200, "headers": {"x-proof": "yes"}, "body": "admin session"},
            "significance": "Confirmed authentication bypass",
        },
    )

    assert response.status_code == 201
    assert response.json()["id"] == "r001"
    assert response.json()["method"] == "POST"
    records = client.get(f"/projects/{project_id}/http-records").json()
    assert records[0]["response"]["status"] == 200
    assert records[0]["significance"] == "Confirmed authentication bypass"


def test_dispatch_config_api_redacts_and_preserves_secrets(client: TestClient, tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(
        """
server: http://127.0.0.1:8000
runtime:
  execution: local
  interval: 3
  max_workers: 1
  max_running_projects: 1
  max_project_workers: 1
  healthcheck_timeout: 5
  worker_healthcheck: disabled
  prompt_group: default
tasks:
  bootstrap: {timeout: 10, conclude_timeout: 5}
  reason: {timeout: 10, max_intents: 1}
  explore: {timeout: 10, conclude_timeout: 5}
local: {completed_action: keep}
workers:
  - name: test
    type: codex
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
    env: {OPENAI_API_KEY: secret-value}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatch_config_store, "_config_path", config_path)

    document = client.get("/dispatch-config").json()
    assert "secret-value" not in document["yaml"]
    assert "********" in document["yaml"]
    updated_yaml = document["yaml"].replace("interval: 3", "interval: 4")
    updated = client.put("/dispatch-config", json={"yaml": updated_yaml})
    assert updated.status_code == 200
    assert updated.json()["restart_required"] is False
    saved = config_path.read_text(encoding="utf-8")
    assert "interval: 4" in saved
    assert "secret-value" in saved
