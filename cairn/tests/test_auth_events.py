from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from pydantic import ValidationError

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import CreateAuthEvent
from cairn.server.services import (
    bootstrap_auth_credentials,
    bootstrap_auth_deployment,
    lookup_auth_credential,
    provision_auth_credential,
    revoke_auth_credential,
    rotate_auth_credential,
    require_secure_bearer_transport,
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES ('proj_001', 'test', 'active', 1, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO auth_requests (id, project_id, source_fact_ids, auth_ref, role, login_url, reason, status, created_at) VALUES ('auth_001', 'proj_001', 'origin', 'target-user', 'user', 'https://example.test/login', 'secret reason', 'pending', '2099-01-01T00:00:00Z')"
        )
        provision_auth_credential(
            conn,
            "helper-token",
            actor_id="helper-a",
            scopes={"helper.event.submit", "helper.request.read"},
            project_allowlist={"proj_001"},
        )
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def _headers(token: str = "helper-token", proto: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if proto:
        headers["X-Forwarded-Proto"] = proto
    return headers


def _event(kind: str = "launch_requested", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": "proj_001",
        "request_id": "auth_001",
        "auth_ref": "target-user",
        "kind": kind,
        "idempotency_key": str(uuid4()),
        "occurred_at": "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    return payload


def test_event_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateAuthEvent.model_validate(_event(actor_id="forged"))


def test_event_ingress_rejects_project_request_auth_ref_mismatch(client: TestClient) -> None:
    response = client.post(
        "/auth-events",
        json={**_event(), "auth_ref": "other-target"},
        headers=_headers(proto="https"),
    )
    assert response.status_code == 409


def test_event_ingress_is_idempotent_per_actor_and_rejects_conflict(client: TestClient) -> None:
    key = str(uuid4())
    payload = _event(idempotency_key=key)
    first = client.post("/auth-events", json=payload, headers=_headers(proto="https"))
    second = client.post("/auth-events", json=payload, headers=_headers(proto="https"))
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()

    conflict = client.post(
        "/auth-events",
        json={**payload, "kind": "browser_opened"},
        headers=_headers(proto="https"),
    )
    assert conflict.status_code == 409


def test_event_retry_from_new_process_ignores_occurred_at_for_idempotency(client: TestClient) -> None:
    key = str(uuid4())
    payload = _event(idempotency_key=key, occurred_at="2026-01-01T00:00:00Z")
    first = client.post("/auth-events", json=payload, headers=_headers(proto="https"))
    assert first.status_code == 201

    # A restarted Helper has no in-memory timestamp cache and may submit a new
    # display timestamp. The durable idempotency key still identifies the event.
    with TestClient(app, base_url="https://testserver") as restarted_client:
        retry = restarted_client.post(
            "/auth-events",
            json={**payload, "occurred_at": "2026-01-01T00:01:00Z"},
            headers=_headers(proto="https"),
        )
    assert retry.status_code == 200
    assert retry.json() == first.json()


def test_helper_view_is_scoped_and_redacts_legacy_fields(client: TestClient) -> None:
    pending = client.get(
        "/projects/proj_001/auth-requests/helper-pending", headers=_headers(proto="https")
    )
    assert pending.status_code == 200
    assert set(pending.json()[0]) == {"id", "auth_ref", "login_url", "status"}
    assert pending.json()[0]["login_url"] is None
    assert "reason" not in pending.json()[0]

    view = client.get(
        "/projects/proj_001/auth-requests/auth_001/helper-view", headers=_headers(proto="https")
    )
    assert view.status_code == 200
    assert set(view.json()) == {"id", "auth_ref", "login_url", "status"}


def test_claimed_helper_view_is_visible_only_to_bound_actor(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE auth_requests SET status = 'claimed', helper_actor_id = 'helper-a' WHERE id = 'auth_001'"
        )
        provision_auth_credential(
            conn,
            "other-helper-token",
            actor_id="helper-b",
            scopes={"helper.request.read"},
            project_allowlist={"proj_001"},
        )

    owner = client.get(
        "/projects/proj_001/auth-requests/auth_001/helper-view", headers=_headers("helper-token", proto="https")
    )
    assert owner.status_code == 200
    assert owner.json()["helper_actor_id"] == "helper-a"

    other = client.get(
        "/projects/proj_001/auth-requests/auth_001/helper-view", headers=_headers("other-helper-token", proto="https")
    )
    assert other.status_code == 404


def test_legacy_helper_listing_remains_available_until_helper_event_migration(client: TestClient) -> None:
    response = client.get("/auth-requests", headers=_headers(proto="https"))
    # Raw helper migration is deferred until AuthHelperClient switches to events in Task 3.
    assert response.status_code == 200


def test_deployment_bootstrap_hashes_helper_and_dispatcher_tokens_without_plaintext(tmp_path, monkeypatch) -> None:
    from cairn.dispatcher.config import AuthConfig

    monkeypatch.setenv("AUTH_HELPER_SECRET", "helper-deployment-secret")
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "bootstrap.db")
    with db.get_conn() as conn:
        config = AuthConfig.model_validate({
            "store_root": "/tmp/auth",
            "helper_token_env": "AUTH_HELPER_SECRET",
            "helper_actor_id": "desktop-helper",
            "helper_scopes": ["helper.event.submit", "helper.request.read"],
            "helper_project_allowlist": ["proj_001", "proj_002"],
            "targets": [{"name": "target-user", "base_url": "https://example.test", "login_url": "https://example.test/login", "role": "user", "verify": {"url": "https://example.test/me"}}],
        })
        bootstrap_auth_deployment(conn, auth_config=config, dispatcher_token="dispatcher-deployment-secret")
        rows = conn.execute("SELECT * FROM auth_credentials ORDER BY actor_id").fetchall()
        assert [row["actor_id"] for row in rows] == ["desktop-helper", "dispatcher"]
        assert all("deployment-secret" not in row["token_digest"] for row in rows)
        helper = next(row for row in rows if row["actor_id"] == "desktop-helper")
        assert helper["project_allowlist"] == '["proj_001", "proj_002"]'


def test_internal_credential_bootstrap_ignores_environment_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "bootstrap-metadata.db")
    monkeypatch.setenv("CAIRN_AUTH_DISPATCHER_ACTOR_ID", "environment-dispatcher")
    monkeypatch.setenv("CAIRN_AUTH_DISPATCHER_SCOPES", "*")
    monkeypatch.setenv("CAIRN_AUTH_DISPATCHER_PROJECTS", "*")
    monkeypatch.setenv("CAIRN_AUTH_HELPER_ACTOR_ID", "environment-helper")
    monkeypatch.setenv("CAIRN_AUTH_HELPER_SCOPES", "*")
    monkeypatch.setenv("CAIRN_AUTH_HELPER_PROJECTS", "*")

    with db.get_conn() as conn:
        bootstrap_auth_credentials(
            conn,
            dispatcher_token="dispatcher-snapshot",
            helper_token="helper-snapshot",
            allow_environment_fallback=False,
        )
        rows = {
            row["actor_id"]: row
            for row in conn.execute("SELECT * FROM auth_credentials ORDER BY actor_id")
        }

    assert rows["dispatcher"]["scopes"] == '["dispatcher.auth.consume"]'
    assert rows["dispatcher"]["project_allowlist"] == '["*"]'
    assert rows["helper"]["scopes"] == '["helper.event.submit", "helper.request.read"]'
    assert rows["helper"]["project_allowlist"] == '["*"]'


def test_helper_view_uses_configured_target_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "target-authority.db")
    with db.get_conn() as conn:
        bootstrap_auth_deployment(conn, target_configs={"target-user": "https://configured.example/login"})
        conn.execute("INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES ('proj_001', 'test', 'active', 1, '2099-01-01T00:00:00Z')")
        conn.execute("INSERT INTO auth_requests (id, project_id, source_fact_ids, auth_ref, role, login_url, reason, status, created_at) VALUES ('auth_001', 'proj_001', 'origin', 'target-user', 'user', 'https://untrusted.example/login', 'secret', 'pending', '2099-01-01T00:00:00Z')")
        provision_auth_credential(conn, "helper-token", actor_id="helper-a", scopes={"helper.request.read"}, project_allowlist={"proj_001"})
    with TestClient(app, base_url="https://testserver") as configured:
        response = configured.get("/projects/proj_001/auth-requests/auth_001/helper-view", headers=_headers())
    assert response.status_code == 200
    assert response.json()["login_url"] == "https://configured.example/login"


def test_target_authority_snapshot_replaces_stale_targets_atomically(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "target-snapshot.db")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO auth_target_configs VALUES ('stale', 'https://stale.example/login')")
        bootstrap_auth_deployment(conn, target_configs={"fresh": "https://fresh.example/login"})
        rows = conn.execute("SELECT auth_ref, login_url FROM auth_target_configs ORDER BY auth_ref").fetchall()
    assert [(row["auth_ref"], row["login_url"]) for row in rows] == [("fresh", "https://fresh.example/login")]


def test_internal_deployment_bootstrap_requires_dispatcher_and_replaces_snapshot(client: TestClient) -> None:
    payload = {
        "dispatcher_token": "dispatcher-token",
        "helper_token": "helper-token-new",
        "helper_actor_id": "desktop-helper",
        "helper_scopes": ["helper.event.submit", "helper.request.read"],
        "helper_project_allowlist": ["proj_001"],
        "targets": {"fresh": "https://fresh.example/login"},
    }
    with db.get_conn() as conn:
        conn.execute("INSERT INTO auth_target_configs VALUES ('stale', 'https://stale.example/login')")
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )

    assert client.post("/internal/auth/deployment", json=payload).status_code == 401
    assert client.post("/internal/auth/deployment", json=payload, headers=_headers()).status_code == 403

    response = client.post(
        "/internal/auth/deployment",
        json=payload,
        headers=_headers("dispatcher-token", proto="https"),
    )
    assert response.status_code == 204
    with db.get_conn() as conn:
        targets = conn.execute("SELECT auth_ref, login_url FROM auth_target_configs").fetchall()
        credentials = conn.execute("SELECT token_digest, actor_id FROM auth_credentials ORDER BY actor_id").fetchall()
    assert [(row["auth_ref"], row["login_url"]) for row in targets] == [("fresh", "https://fresh.example/login")]
    assert ("dispatcher-token" not in str(credentials))
    assert any(row["actor_id"] == "desktop-helper" for row in credentials)


def test_internal_deployment_rejects_equal_helper_and_dispatcher_tokens_without_mutation(
    client: TestClient,
) -> None:
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )
        conn.execute("INSERT INTO auth_target_configs VALUES ('stale', 'https://stale.example/login')")

    response = client.post(
        "/internal/auth/deployment",
        json={
            "dispatcher_token": "dispatcher-token",
            "helper_token": "dispatcher-token",
            "targets": {"fresh": "https://fresh.example/login"},
        },
        headers=_headers("dispatcher-token", proto="https"),
    )

    assert response.status_code == 422
    with db.get_conn() as conn:
        # The fixture's helper credential is unrelated and must remain untouched.
        assert conn.execute("SELECT COUNT(*) FROM auth_credentials").fetchone()[0] == 2
        rows = conn.execute("SELECT auth_ref FROM auth_target_configs").fetchall()
    assert [row["auth_ref"] for row in rows] == ["stale"]


def test_internal_deployment_rejects_digest_collision_without_changing_existing_authority(
    client: TestClient,
) -> None:
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )
        provision_auth_credential(
            conn,
            "collision-token",
            actor_id="operator",
            scopes={"ui.auth.read"},
            project_allowlist={"proj_001"},
        )

    response = client.post(
        "/internal/auth/deployment",
        json={
            "dispatcher_token": "dispatcher-token",
            "helper_token": "collision-token",
            "helper_actor_id": "desktop-helper",
            "helper_scopes": ["helper.event.submit"],
            "helper_project_allowlist": ["*"],
        },
        headers=_headers("dispatcher-token", proto="https"),
    )

    assert response.status_code == 422
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT actor_id, scopes, project_allowlist FROM auth_credentials WHERE token_digest = ?",
            (hashlib.sha256(b"collision-token").hexdigest(),),
        ).fetchone()
    assert row["actor_id"] == "operator"
    assert row["scopes"] == '["ui.auth.read"]'
    assert row["project_allowlist"] == '["proj_001"]'


def test_deployment_snapshot_reconciles_omitted_credentials_and_preserves_manual_credentials(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "deployment-reconcile.db")
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "manual-token",
            actor_id="operator",
            scopes={"ui.auth.read"},
            project_allowlist={"*"},
        )
        bootstrap_auth_deployment(
            conn,
            dispatcher_token="dispatcher-token",
            helper_token="helper-token",
            allow_environment_fallback=False,
        )
        bootstrap_auth_deployment(
            conn,
            dispatcher_token="dispatcher-token",
            allow_environment_fallback=False,
        )
        helper = lookup_auth_credential(conn, "helper-token")
        manual = lookup_auth_credential(conn, "manual-token")

    assert helper is None
    assert manual is not None


def test_acknowledged_legacy_cutover_revokes_unowned_custom_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "legacy-cutover.db")
    monkeypatch.setenv("CAIRN_AUTH_LEGACY_CREDENTIAL_CUTOVER", "revoke")
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "custom-legacy-token",
            actor_id="desktop-helper-v1",
            scopes={"helper.event.submit"},
            project_allowlist={"*"},
        )
        bootstrap_auth_deployment(
            conn,
            dispatcher_token="dispatcher-token",
        )
        assert lookup_auth_credential(conn, "custom-legacy-token") is None
        audit = conn.execute("SELECT * FROM auth_credential_cutovers").fetchall()
        assert len(audit) == 1
        assert audit[0]["revoked_count"] == 1

        # Re-running with the acknowledgement still present is a no-op.
        bootstrap_auth_deployment(
            conn,
            dispatcher_token="dispatcher-token",
            allow_environment_fallback=False,
        )
        assert conn.execute("SELECT COUNT(*) FROM auth_credential_cutovers").fetchone()[0] == 1


def test_deployment_credential_rotation_has_bounded_current_previous_overlap(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "deployment-rotation.db")
    with db.get_conn() as conn:
        bootstrap_auth_deployment(
            conn,
            dispatcher_token="dispatcher-old",
            helper_token="helper-old",
            allow_environment_fallback=False,
        )
        bootstrap_auth_deployment(
            conn,
            dispatcher_token="dispatcher-new",
            helper_token="helper-new",
            allow_environment_fallback=False,
        )
        assert lookup_auth_credential(conn, "dispatcher-old") is not None
        assert lookup_auth_credential(conn, "helper-old") is not None
        assert lookup_auth_credential(conn, "dispatcher-new") is not None
        assert lookup_auth_credential(conn, "helper-new") is not None

        old_dispatcher = lookup_auth_credential(conn, "dispatcher-old")
        old_helper = lookup_auth_credential(conn, "helper-old")
        assert old_dispatcher is not None and old_helper is not None
        assert old_dispatcher.credential_id != old_helper.credential_id
        rows = conn.execute(
            "SELECT token_digest, expires_at FROM auth_credentials WHERE token_digest IN (?, ?)",
            (
                hashlib.sha256(b"dispatcher-old").hexdigest(),
                hashlib.sha256(b"helper-old").hexdigest(),
            ),
        ).fetchall()
        now = datetime.now(timezone.utc)
        for row in rows:
            expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            assert now < expiry <= now + timedelta(seconds=300)


def test_malformed_environment_snapshot_fails_before_mutating_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "malformed-snapshot.db")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO auth_target_configs VALUES ('stale', 'https://stale.example/login')")
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )

    monkeypatch.setenv(
        "CAIRN_AUTH_DEPLOYMENT_SNAPSHOT",
        '{"dispatcher_token":"dispatcher-token","helper_scopes":["valid", 7],'
        '"targets":{"fresh":"https://fresh.example/login","bad":"http://bad.example/login"}}',
    )
    with db.get_conn() as conn:
        with pytest.raises(ValueError):
            bootstrap_auth_deployment(conn)

    with db.get_conn() as conn:
        targets = conn.execute("SELECT auth_ref, login_url FROM auth_target_configs").fetchall()
        assert lookup_auth_credential(conn, "dispatcher-token") is not None
    assert [(row["auth_ref"], row["login_url"]) for row in targets] == [
        ("stale", "https://stale.example/login")
    ]


@pytest.mark.parametrize(
    ("token", "actor_id", "scopes"),
    [
        ("custom-dispatcher-scope-token", "custom-actor", {"dispatcher.auth.consume"}),
        ("custom-wildcard-token", "custom-actor", {"*"}),
    ],
)
def test_internal_deployment_requires_unambiguous_dispatcher_identity(
    client: TestClient, token: str, actor_id: str, scopes: set[str]
) -> None:
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            token,
            actor_id=actor_id,
            scopes=scopes,
            project_allowlist={"*"},
        )

    response = client.post(
        "/internal/auth/deployment",
        json={"targets": {"custom": "https://custom.example/login"}},
        headers=_headers(token, proto="https"),
    )

    assert response.status_code == 403


def test_internal_deployment_bootstrap_rejects_invalid_snapshot_without_partial_update(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("INSERT INTO auth_target_configs VALUES ('stale', 'https://stale.example/login')")
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )

    response = client.post(
        "/internal/auth/deployment",
        json={
            "dispatcher_token": "dispatcher-token",
            "targets": {
                "fresh": "https://fresh.example/login",
                "bad": "http://insecure.example/login",
            },
        },
        headers=_headers("dispatcher-token", proto="https"),
    )
    assert response.status_code == 422
    with db.get_conn() as conn:
        rows = conn.execute("SELECT auth_ref, login_url FROM auth_target_configs").fetchall()
    assert [(row["auth_ref"], row["login_url"]) for row in rows] == [
        ("stale", "https://stale.example/login")
    ]


def test_internal_deployment_bootstrap_uses_request_snapshot_over_server_environment(client: TestClient, monkeypatch) -> None:
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )
    monkeypatch.setenv(
        "CAIRN_AUTH_DEPLOYMENT_SNAPSHOT",
        '{"dispatcher_token":"wrong-token","targets":{"environment":"https://environment.example/login"}}',
    )

    response = client.post(
        "/internal/auth/deployment",
        json={
            "dispatcher_token": "dispatcher-token",
            "targets": {"request": "https://request.example/login"},
        },
        headers=_headers("dispatcher-token", proto="https"),
    )
    assert response.status_code == 204
    with db.get_conn() as conn:
        rows = conn.execute("SELECT auth_ref, login_url FROM auth_target_configs").fetchall()
    assert [(row["auth_ref"], row["login_url"]) for row in rows] == [
        ("request", "https://request.example/login")
    ]


def test_internal_deployment_bootstrap_does_not_fall_back_to_helper_environment(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("CAIRN_AUTH_HELPER_TOKEN", "environment-helper-secret")
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )

    response = client.post(
        "/internal/auth/deployment",
        json={"dispatcher_token": "dispatcher-token"},
        headers=_headers("dispatcher-token", proto="https"),
    )

    assert response.status_code == 204
    with db.get_conn() as conn:
        assert lookup_auth_credential(conn, "environment-helper-secret") is None


def test_internal_deployment_bootstrap_does_not_fall_back_to_dispatcher_environment(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("CAIRN_AUTH_DISPATCHER_TOKEN", "environment-dispatcher-secret")
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )

    response = client.post(
        "/internal/auth/deployment",
        json={},
        headers=_headers("dispatcher-token", proto="https"),
    )

    assert response.status_code == 204
    with db.get_conn() as conn:
        assert lookup_auth_credential(conn, "environment-dispatcher-secret") is None


def test_internal_deployment_requires_server_seed_before_first_authenticated_call(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.delenv("CAIRN_AUTH_DISPATCHER_TOKEN", raising=False)
    db.configure(tmp_path / "first-run.db")

    with TestClient(app, base_url="https://testserver") as test_client:
        denied = test_client.post(
            "/internal/auth/deployment",
            json={"dispatcher_token": "dispatcher-token"},
            headers=_headers("dispatcher-token", proto="https"),
        )
        assert denied.status_code == 401

        # The Server operator performs this deployment-only seed before starting
        # DispatcherLoop; no unauthenticated API bootstrap is available.
        monkeypatch.setenv("CAIRN_AUTH_DISPATCHER_TOKEN", "dispatcher-token")
        with db.get_conn() as conn:
            bootstrap_auth_deployment(conn)

        accepted = test_client.post(
            "/internal/auth/deployment",
            json={"dispatcher_token": "dispatcher-token"},
            headers=_headers("dispatcher-token", proto="https"),
        )
        assert accepted.status_code == 204


def test_transport_guard_rejects_non_loopback_cleartext(client: TestClient) -> None:
    with TestClient(app, base_url="http://remote.example") as insecure:
        response = insecure.post("/auth-events", json=_event(), headers=_headers())
    assert response.status_code == 400


def test_transport_guard_rejects_untrusted_forwarded_https() -> None:
    scope = {
        "type": "http", "scheme": "http", "server": ("remote.example", 80),
        "client": ("203.0.113.10", 12345), "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": [(b"x-forwarded-proto", b"https")], "http_version": "1.1",
    }
    with pytest.raises(Exception):
        require_secure_bearer_transport(Request(scope))


def test_transport_guard_accepts_actual_loopback_cleartext() -> None:
    scope = {
        "type": "http", "scheme": "http", "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 12345), "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": [], "http_version": "1.1",
    }
    require_secure_bearer_transport(Request(scope))


def test_helper_views_reap_stale_claims_and_requests(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_requests SET status = 'claimed', claimed_at = '2000-01-01T00:00:00Z' WHERE id = 'auth_001'")
        conn.execute("UPDATE auth_requests SET created_at = '2000-01-01T00:00:00Z' WHERE id = 'auth_001'")
    result = client.get("/projects/proj_001/auth-requests/helper-pending", headers=_headers())
    assert result.status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM auth_requests WHERE id = 'auth_001'").fetchone()["status"] == "expired"


def test_credential_digest_rotation_scope_and_revocation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "credentials.db")
    with db.get_conn() as conn:
        credential = provision_auth_credential(
            conn,
            "current-token",
            actor_id="helper-a",
            scopes={"helper.event.submit"},
            project_allowlist={"proj_001"},
        )
        assert credential.token_digest == hashlib.sha256(b"current-token").hexdigest()
        assert credential.actor_id == "helper-a"

        principal = lookup_auth_credential(conn, "current-token")
        assert principal is not None
        assert principal.actor_id == "helper-a"
        assert principal.project_allowlist == frozenset({"proj_001"})

        rotated = rotate_auth_credential(conn, "current-token", "next-token", overlap_seconds=300)
        assert lookup_auth_credential(conn, "current-token") is not None
        assert lookup_auth_credential(conn, "next-token") is not None
        assert rotated.actor_id == "helper-a"
        assert revoke_auth_credential(conn, "next-token") is True
        assert lookup_auth_credential(conn, "next-token") is None
