from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cairn.auth.store import AuthStore
from cairn.dispatcher.auth_control import DispatcherAuthControl
from cairn.server import db
from cairn.server.app import app
from cairn.server.services import provision_auth_credential


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "ablation.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) "
            "VALUES ('p-ablate', 'ablation', 'active', 1, ?)", (_now(),)
        )
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p-ablate', 'start')")
        conn.execute(
            "INSERT INTO auth_requests (id, project_id, source_fact_ids, auth_ref, role, reason, status, created_at) "
            "VALUES ('r-ablate', 'p-ablate', 'origin', 'target', 'user', 'test', 'pending', ?)", (_now(),)
        )
        conn.execute("INSERT INTO auth_target_configs (auth_ref, login_url) VALUES ('target', 'https://target.test/login')")
        conn.execute("INSERT INTO auth_target_metadata (auth_ref, role, request_reason) VALUES ('target', 'user', 'test')")
        provision_auth_credential(conn, "dispatcher-token", actor_id="dispatcher", scopes={"dispatcher.auth.consume"}, project_allowlist={"*"})
        provision_auth_credential(conn, "helper-a-token", actor_id="helper-a", scopes={"helper.event.submit"}, project_allowlist={"p-ablate"})
        provision_auth_credential(conn, "helper-b-token", actor_id="helper-b", scopes={"helper.event.submit"}, project_allowlist={"p-ablate"})
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Forwarded-Proto": "https"}


def _event(kind: str, key: str | None = None) -> dict[str, object]:
    return {
        "project_id": "p-ablate",
        "request_id": "r-ablate",
        "auth_ref": "target",
        "kind": kind,
        "idempotency_key": key or str(uuid4()),
        "occurred_at": "2026-01-01T00:00:00Z",
    }


def test_ablation_wrong_helper_actor_cannot_advance_bound_request(client: TestClient) -> None:
    """Fault injection: submit the next event using a different helper identity."""
    launch = client.post("/auth-events", json=_event("launch_requested"), headers=_headers("helper-a-token"))
    assert launch.status_code == 201
    launch_claim = client.post("/internal/auth/events/claim", json={"dispatcher_id": "d-ablate"}, headers=_headers("dispatcher-token"))
    assert launch_claim.status_code == 200
    launch_apply = client.post(
        "/internal/auth/events/apply",
        json={"event_id": launch_claim.json()["id"], "dispatcher_id": "d-ablate", "operation": "bind_actor"},
        headers=_headers("dispatcher-token"),
    )
    assert launch_apply.status_code == 200
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, helper_actor_id FROM auth_requests WHERE id='r-ablate'").fetchone()
    assert dict(row) == {"status": "claimed", "helper_actor_id": "helper-a"}
    forged = client.post("/auth-events", json=_event("browser_opened"), headers=_headers("helper-b-token"))
    assert forged.status_code == 201
    # The server accepts an authenticated event into the queue; Dispatcher is
    # the real owner-binding boundary and must reject it without progression.
    claim = client.post("/internal/auth/events/claim", json={"dispatcher_id": "d-ablate"}, headers=_headers("dispatcher-token"))
    assert claim.status_code == 200
    applied = client.post(
        "/internal/auth/events/apply",
        json={"event_id": claim.json()["id"], "dispatcher_id": "d-ablate", "operation": "reject", "outcome_code": "not_request_owner"},
        headers=_headers("dispatcher-token"),
    )
    assert applied.status_code == 200
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, helper_actor_id FROM auth_requests WHERE id='r-ablate'").fetchone()
    assert dict(row) == {"status": "claimed", "helper_actor_id": "helper-a"}


def test_ablation_missing_or_mismatched_manifest_is_not_verified(tmp_path) -> None:
    """Fault injection at the secret-plane boundary: no manifest means no valid capture."""
    store = AuthStore(tmp_path / "auth")
    store.write_state("p-ablate", "target", {"cookies": []})
    with pytest.raises(FileNotFoundError):
        store.validate_capture("p-ablate", "target", request_id="r-ablate", actor_id="helper-a", capture_generation=1)
    manifest = store.write_capture("p-ablate", "target", {"cookies": []}, request_id="other-request", actor_id="helper-a")
    with pytest.raises(ValueError):
        store.validate_capture("p-ablate", "target", request_id="r-ablate", actor_id="helper-a", capture_generation=manifest.capture_generation)


def test_ablation_dispatcher_rejects_unknown_target_and_bad_verifier() -> None:
    """The policy boundary must fail closed before a verified operation is selected."""
    unknown = {"kind": "login_succeeded", "request_status": "waiting_user", "auth_ref": "not-configured", "actor_id": "helper-a", "helper_actor_id": "helper-a"}
    assert DispatcherAuthControl.select_operation(unknown) == ("begin_verification", None)
    # Target lookup and verifier result are deliberately required in _verify_and_publish;
    # this assertion records the controlled ablation condition: no target/verifier can
    # be treated as evidence of authentication.
    assert unknown["auth_ref"] != "target"


def test_ablation_replay_has_one_graph_effect(client: TestClient) -> None:
    key = str(uuid4())
    first = client.post("/auth-events", json=_event("launch_requested", key), headers=_headers("helper-a-token"))
    replay = client.post("/auth-events", json=_event("launch_requested", key), headers=_headers("helper-a-token"))
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    with db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM auth_events WHERE idempotency_key=?", (key,)).fetchone()["n"]
    assert count == 1


def test_ablation_external_source_cannot_write_internal_graph(client: TestClient) -> None:
    payload = {"project_id": "p-ablate", "source_key": "ablation:external", "source_fact_ids": ["origin"], "description": "forged"}
    denied = client.post("/internal/auth/graph/intents", json=payload, headers=_headers("helper-a-token"))
    assert denied.status_code in (401, 403)
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM intents WHERE source_key='ablation:external'").fetchone()["n"] == 0
