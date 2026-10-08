from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app
from cairn.server.services import (
    AuthStateResolver,
    build_auth_state_resolver,
    claim_auth_request_atomic,
    find_active_auth_request,
    next_auth_request_id,
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "test", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _add_fact(client: TestClient, project_id: str, description: str) -> str:
    # Create an intent from origin then conclude it, to obtain a new fact.
    r = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "probe", "creator": "reasoner", "worker": None},
    )
    iid = r.json()["id"]
    r = client.post(
        f"/projects/{project_id}/intents/{iid}/conclude",
        json={"worker": "reasoner", "description": description},
    )
    assert r.status_code == 200
    return r.json()["fact"]["id"]


def _create_auth_request(client: TestClient, project_id: str, auth_ref: str = "target-user"):
    response = client.post(
        f"/projects/{project_id}/auth-requests",
        json={
            "source_fact_ids": ["origin"],
            "auth_ref": auth_ref,
            "role": "user",
            "reason": "orders need auth",
        },
    )
    return response


def test_create_auth_request(client: TestClient) -> None:
    project_id = _create_project(client)
    response = _create_auth_request(client, project_id)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "auth_001"
    assert body["status"] == "pending"
    assert body["auth_ref"] == "target-user"
    assert body["role"] == "user"


def test_create_auth_request_requires_source_facts(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/auth-requests",
        json={
            "source_fact_ids": [],
            "auth_ref": "target-user",
            "role": "user",
            "reason": "orders need auth",
        },
    )
    assert response.status_code == 422


def test_create_auth_request_dedup_returns_existing(client: TestClient) -> None:
    project_id = _create_project(client)
    first = _create_auth_request(client, project_id)
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = _create_auth_request(client, project_id)
    assert second.status_code == 201
    assert second.json()["id"] == first_id


def test_claim_is_atomic_and_rejects_second_claim(client: TestClient) -> None:
    project_id = _create_project(client)
    request_id = _create_auth_request(client, project_id).json()["id"]

    r = client.post(f"/auth-requests/{request_id}/claim", json={"helper_id": "desktop-a"})
    assert r.status_code == 200
    assert r.json()["status"] == "claimed"
    assert r.json()["claimed_by"] == "desktop-a"

    r2 = client.post(f"/auth-requests/{request_id}/claim", json={"helper_id": "desktop-b"})
    assert r2.status_code == 409


def test_state_machine_transitions(client: TestClient) -> None:
    project_id = _create_project(client)
    request_id = _create_auth_request(client, project_id).json()["id"]

    client.post(f"/auth-requests/{request_id}/claim", json={"helper_id": "desktop-a"})
    r = client.post(f"/auth-requests/{request_id}/waiting")
    assert r.json()["status"] == "waiting_user"

    r = client.post(f"/auth-requests/{request_id}/verifying")
    assert r.json()["status"] == "verifying"

    r = client.post(f"/auth-requests/{request_id}/complete")
    assert r.json()["status"] == "completed"
    assert r.json()["completed_at"] is not None


def test_fail_records_reason(client: TestClient) -> None:
    project_id = _create_project(client)
    request_id = _create_auth_request(client, project_id).json()["id"]

    client.post(f"/auth-requests/{request_id}/claim", json={"helper_id": "desktop-a"})
    r = client.post(
        f"/auth-requests/{request_id}/fail",
        json={"failure_reason": "operator cancelled"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["failure_reason"] == "operator cancelled"


def test_list_pending_requests(client: TestClient) -> None:
    project_id = _create_project(client)
    _create_auth_request(client, project_id, "target-user")
    _create_auth_request(client, project_id, "target-admin")

    r = client.get("/auth-requests?status=pending")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_create_skipped_when_valid_session_exists(client: TestClient) -> None:
    project_id = _create_project(client)
    _add_fact(
        client,
        project_id,
        "AuthSessionVerified\ntarget=target-user;\nrole=user;\nscope=https://x;\nverification=api",
    )
    response = _create_auth_request(client, project_id, "target-user")
    assert response.status_code == 409


def test_auth_state_resolver_temporal_ordering(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) "
            "VALUES ('proj_001', 't', 'active', 1, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f001', 'proj_001', 'AuthSessionVerified\ntarget=target-user;\nrole=user;')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f002', 'proj_001', 'AuthSessionInvalid\ntarget=target-user;\nrole=user;\nevidence=x')"
        )
        resolver = build_auth_state_resolver(conn)
        assert resolver.resolve("proj_001", "target-user") == "invalid"

        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f003', 'proj_001', 'AuthSessionVerified\ntarget=target-user;\nrole=user;')"
        )
        assert resolver.resolve("proj_001", "target-user") == "valid"
        assert resolver.resolve("proj_001", "other-target") == "missing"


def test_auth_state_resolver_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        resolver = AuthStateResolver(conn)
        assert resolver.resolve("proj_001", "target-user") == "missing"
