from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.services import provision_auth_credential
from cairn.dispatcher.auth_control import DispatcherAuthControl
from cairn.dispatcher.protocol.client import ApiResult, ProtocolError


def _ts(seconds_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) "
            "VALUES ('p1', 'test', 'active', 1, ?)" , (_ts(),)
        )
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p1', 'start')")
        conn.execute(
            "INSERT INTO auth_target_configs (auth_ref, login_url) VALUES ('new-target', 'https://target.example/login')"
        )
        conn.execute(
            "INSERT INTO auth_target_metadata (auth_ref, role, request_reason) VALUES ('new-target', 'admin', 'authentication_required')"
        )
        conn.execute(
            "INSERT INTO auth_requests "
            "(id, project_id, source_fact_ids, auth_ref, role, reason, status, created_at) "
            "VALUES ('r1', 'p1', 'origin', 'target', 'user', 'need auth', 'pending', ?)" , (_ts(),)
        )
        provision_auth_credential(
            conn,
            "dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"*"},
        )
        conn.execute(
            "INSERT INTO auth_events "
            "(id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key, "
            "occurred_at, received_at, state) VALUES "
            "('e1', 'p1', 'r1', 'target', 'launch_requested', 'helper-a', ?, ?, ?, 'queued')",
            (str(uuid4()), _ts(), _ts()),
        )
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def _headers(token: str = "dispatcher-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Forwarded-Proto": "https"}


def test_dispatcher_claim_is_atomic_and_exhausts_after_three_attempts(client: TestClient) -> None:
    first = client.post(
        "/internal/auth/events/claim",
        json={"dispatcher_id": "d1"},
        headers=_headers(),
    )
    assert first.status_code == 200
    assert first.json()["state"] == "claimed"
    assert first.json()["attempt_count"] == 1

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE auth_events SET claim_expires_at = ? WHERE id = 'e1'", (_ts(60),)
        )
    client.post("/internal/auth/events/recover", json={}, headers=_headers())
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_events SET next_attempt_at = ? WHERE id = 'e1'", (_ts(),))
    client.post("/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers())
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_events SET claim_expires_at = ? WHERE id = 'e1'", (_ts(60),))
    client.post("/internal/auth/events/recover", json={}, headers=_headers())
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_events SET next_attempt_at = ? WHERE id = 'e1'", (_ts(),))
    client.post("/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers())
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_events SET claim_expires_at = ? WHERE id = 'e1'", (_ts(60),))
    client.post("/internal/auth/events/recover", json={}, headers=_headers())
    with db.get_conn() as conn:
        row = conn.execute("SELECT state, attempt_count, outcome_code FROM auth_events WHERE id = 'e1'").fetchone()
    assert (row["state"], row["attempt_count"], row["outcome_code"]) == ("rejected", 3, "retry_exhausted")


def test_internal_apply_binds_actor_and_rejects_wrong_actor(client: TestClient) -> None:
    claim = client.post("/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers())
    assert claim.status_code == 200
    applied = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e1", "dispatcher_id": "d1", "operation": "bind_actor"},
        headers=_headers(),
    )
    assert applied.status_code == 200
    assert applied.json()["status"] == "claimed"
    with db.get_conn() as conn:
        row = conn.execute("SELECT helper_actor_id FROM auth_requests WHERE id = 'r1'").fetchone()
        conn.execute(
            "INSERT INTO auth_events "
            "(id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key, occurred_at, received_at, state) "
            "VALUES ('e2', 'p1', 'r1', 'target', 'browser_opened', 'helper-b', ?, ?, ?, 'queued')",
            (str(uuid4()), _ts(), _ts()),
        )
    assert row["helper_actor_id"] == "helper-a"

    client.post("/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers())
    rejected = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e2", "dispatcher_id": "d1", "operation": "reject", "outcome_code": "not_request_owner"},
        headers=_headers(),
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"


def test_login_succeeded_can_resume_verification_on_the_same_claimed_event(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE auth_requests SET status = 'waiting_user', helper_actor_id = 'helper-a' WHERE id = 'r1'"
        )
        conn.execute(
            "INSERT INTO auth_events "
            "(id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key, "
            "occurred_at, received_at, state) VALUES "
            "('e-login', 'p1', 'r1', 'target', 'login_succeeded', 'helper-a', ?, ?, ?, 'queued')",
            (str(uuid4()), _ts(), _ts(10)),
        )

    claim = client.post("/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers())
    assert claim.status_code == 200
    first = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e-login", "dispatcher_id": "d1", "operation": "begin_verification"},
        headers=_headers(),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "verifying"
    assert first.json()["state"] == "claimed"

    second = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e-login", "dispatcher_id": "d1", "operation": "begin_verification"},
        headers=_headers(),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "verifying"
    assert second.json()["state"] == "claimed"


def test_exhausted_verification_claim_fails_request_even_when_ttl_disabled(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET auth_request_ttl = 0 WHERE rowid = 1")
        conn.execute(
            "UPDATE auth_requests SET status = 'verifying', helper_actor_id = 'helper-a' WHERE id = 'r1'"
        )
        conn.execute(
            "INSERT INTO auth_events "
            "(id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key, "
            "occurred_at, received_at, state, attempt_count, claim_expires_at, claimed_by) VALUES "
            "('e-login-recovery', 'p1', 'r1', 'target', 'login_succeeded', 'helper-a', ?, ?, ?, 'claimed', 3, ?, 'd1')",
            (str(uuid4()), _ts(), _ts(), _ts(60)),
        )
        conn.execute(
            "INSERT INTO auth_lifecycle_events "
            "(request_id, event_id, sequence, kind, recorded_at, outcome_code) "
            "VALUES ('r1', 'e-login-recovery', 1, 'verifying', ?, 'verifying')",
            (_ts(),),
        )

    recovered = client.post("/internal/auth/events/recover", json={}, headers=_headers())
    assert recovered.status_code == 200
    assert recovered.json() == {"recovered": 1}

    with db.get_conn() as conn:
        event = conn.execute(
            "SELECT state, outcome_code, claimed_by, claim_expires_at FROM auth_events WHERE id = 'e-login-recovery'"
        ).fetchone()
        request = conn.execute(
            "SELECT status, failure_reason, helper_actor_id, claimed_by, completed_at, expires_at "
            "FROM auth_requests WHERE id = 'r1'"
        ).fetchone()
        lifecycle = conn.execute(
            "SELECT kind, outcome_code FROM auth_lifecycle_events WHERE request_id = 'r1' ORDER BY sequence"
        ).fetchall()

    assert dict(event) == {
        "state": "rejected",
        "outcome_code": "retry_exhausted",
        "claimed_by": None,
        "claim_expires_at": None,
    }
    assert request["status"] == "failed"
    assert request["failure_reason"] == "verification_failed"
    assert request["helper_actor_id"] is None
    assert request["claimed_by"] is None
    assert request["completed_at"] is not None
    assert request["expires_at"] is None
    assert [(row["kind"], row["outcome_code"]) for row in lifecycle] == [
        ("verifying", "verifying"),
        ("retry_exhausted", "retry_exhausted"),
        ("failed", "verification_failed"),
    ]


def test_login_succeeded_second_event_is_terminally_rejected(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_events SET state = 'rejected' WHERE id = 'e1'")
        conn.execute(
            "UPDATE auth_requests SET status = 'waiting_user', helper_actor_id = 'helper-a' WHERE id = 'r1'"
        )
        conn.execute(
            "INSERT INTO auth_events "
            "(id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key, "
            "occurred_at, received_at, state) VALUES "
            "('e-login-1', 'p1', 'r1', 'target', 'login_succeeded', 'helper-a', ?, ?, ?, 'queued')",
            (str(uuid4()), _ts(), _ts(10)),
        )

    claim = client.post("/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers())
    assert claim.status_code == 200
    first = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e-login-1", "dispatcher_id": "d1", "operation": "begin_verification"},
        headers=_headers(),
    )
    assert first.status_code == 200

    with db.get_conn() as conn:
        # The first event has completed its Dispatcher operation; the lifecycle
        # row still records that it owns the verifying transition.
        conn.execute("UPDATE auth_events SET state = 'applied' WHERE id = 'e-login-1'")
        conn.execute(
            "INSERT INTO auth_events "
            "(id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key, "
            "occurred_at, received_at, state) VALUES "
            "('e-login-2', 'p1', 'r1', 'target', 'login_succeeded', 'helper-a', ?, ?, ?, 'queued')",
            (str(uuid4()), _ts(), _ts()),
        )

    second_claim = client.post("/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers())
    assert second_claim.status_code == 200
    second = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e-login-2", "dispatcher_id": "d1", "operation": "begin_verification"},
        headers=_headers(),
    )
    assert second.status_code == 200
    assert second.json()["state"] == "rejected"
    assert second.json()["event"]["outcome_code"] == "invalid_transition"


def test_internal_endpoints_require_dispatcher_credential(client: TestClient) -> None:
    assert client.post("/internal/auth/events/claim", json={}, headers={}).status_code == 401
    assert client.post(
        "/internal/auth/events/claim", json={}, headers={"Authorization": "Bearer helper-token"}
    ).status_code == 401


def test_internal_apply_requires_dispatcher_selected_operation(client: TestClient) -> None:
    claim = client.post(
        "/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers()
    )
    assert claim.status_code == 200
    missing = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e1", "dispatcher_id": "d1"},
        headers=_headers(),
    )
    assert missing.status_code == 422

    wrong = client.post(
        "/internal/auth/events/apply",
        json={"event_id": "e1", "dispatcher_id": "d1", "operation": "mark_waiting_user"},
        headers=_headers(),
    )
    assert wrong.status_code == 409
    with db.get_conn() as conn:
        row = conn.execute("SELECT state, status FROM auth_events JOIN auth_requests ON auth_requests.id = auth_events.request_id WHERE auth_events.id = 'e1'").fetchone()
    assert (row["state"], row["status"]) == ("claimed", "pending")


def test_internal_apply_rejects_raw_failure_text(client: TestClient) -> None:
    claim = client.post(
        "/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers()
    )
    assert claim.status_code == 200
    response = client.post(
        "/internal/auth/events/apply",
        json={
            "event_id": "e1",
            "dispatcher_id": "d1",
            "operation": "reject",
            "outcome_code": "password leaked to server",
        },
        headers=_headers(),
    )
    assert response.status_code == 422


def test_internal_apply_rejects_outcome_for_the_wrong_operation(client: TestClient) -> None:
    claim = client.post(
        "/internal/auth/events/claim", json={"dispatcher_id": "d1"}, headers=_headers()
    )
    assert claim.status_code == 200
    response = client.post(
        "/internal/auth/events/apply",
        json={
            "event_id": "e1",
            "dispatcher_id": "d1",
            "operation": "bind_actor",
            "outcome_code": "verified",
        },
        headers=_headers(),
    )
    assert response.status_code == 409


def test_dispatcher_selects_operations_and_never_defaults_login_to_verified() -> None:
    assert DispatcherAuthControl.select_operation(
        {"kind": "launch_requested", "request_status": "pending"}
    ) == ("bind_actor", None)
    assert DispatcherAuthControl.select_operation(
        {"kind": "browser_opened", "request_status": "claimed"}
    ) == ("mark_waiting_user", None)
    assert DispatcherAuthControl.select_operation(
        {"kind": "login_succeeded", "request_status": "waiting_user"}
    ) == ("begin_verification", None)
    assert DispatcherAuthControl.select_operation(
        {"kind": "login_failed", "request_status": "verifying"}
    ) == ("mark_failed", "login_failed")
    assert DispatcherAuthControl.select_operation(
        {"kind": "login_succeeded", "request_status": "completed"}
    ) == ("reject", "invalid_transition")


def test_dispatcher_control_cycle_failure_is_reported() -> None:
    class Client:
        def recover_auth_events(self) -> ApiResult:
            return ApiResult(503, text="down")

        def expire_auth_requests(self) -> ApiResult:
            return ApiResult(200, data={"expired": 0})

        def claim_auth_event(self, dispatcher_id: str) -> ApiResult:
            raise AssertionError("must not consume after recovery failure")

    control = DispatcherAuthControl(Client())
    results = control.run_cycle()
    assert results[0].status_code == 503


def test_dispatcher_rejects_mismatched_helper_actor_terminally() -> None:
    calls: list[dict[str, object]] = []

    class Client:
        def claim_auth_event(self, dispatcher_id: str) -> ApiResult:
            return ApiResult(
                200,
                data={
                    "id": "event-1",
                    "kind": "browser_opened",
                    "actor_id": "helper-b",
                    "request_status": "claimed",
                    "helper_actor_id": "helper-a",
                },
            )

        def apply_auth_event(self, event_id: str, dispatcher_id: str, **kwargs: str) -> ApiResult:
            calls.append(kwargs)
            return ApiResult(200, data={"state": "rejected"})

    result = DispatcherAuthControl(Client()).consume_once()
    assert result is not None and result.ok
    assert calls == [{"operation": "reject", "outcome_code": "not_request_owner"}]


def test_dispatcher_claim_endpoint_failure_is_not_empty_queue() -> None:
    class Client:
        def claim_auth_event(self, dispatcher_id: str) -> ApiResult:
            return ApiResult(404, text="route missing")

    with pytest.raises(ProtocolError):
        DispatcherAuthControl(Client()).consume_once()


def test_dispatcher_loop_treats_auth_control_failure_as_a_gate() -> None:
    from cairn.dispatcher.scheduler.loop import DispatcherLoop

    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = SimpleNamespace(auth_control_plane_mode="dual_write")
    loop.auth_control = SimpleNamespace(
        run_cycle=lambda: [ApiResult(503, text="down")]
    )
    assert loop._run_auth_control_cycle() is False


def test_dual_write_denies_migrated_helper_raw_listing(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET auth_control_plane_mode = 'dual_write' WHERE rowid = 1")
        provision_auth_credential(
            conn,
            "helper-token",
            actor_id="helper-a",
            scopes={"helper.event.submit", "helper.request.read"},
            project_allowlist={"p1"},
        )
    response = client.get(
        "/auth-requests", headers={"Authorization": "Bearer helper-token"}
    )
    assert response.status_code == 403


def test_dual_write_helper_read_does_not_reap_stale_claim(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET auth_control_plane_mode = 'dual_write' WHERE rowid = 1")
        provision_auth_credential(
            conn,
            "helper-token",
            actor_id="helper-a",
            scopes={"helper.event.submit", "helper.request.read"},
            project_allowlist={"p1"},
        )
        conn.execute(
            "UPDATE auth_requests SET status = 'claimed', claimed_at = ?, helper_actor_id = 'helper-a' WHERE id = 'r1'",
            (_ts(600),),
        )

    response = client.get(
        "/projects/p1/auth-requests/helper-pending",
        headers={"Authorization": "Bearer helper-token"},
    )
    assert response.status_code == 200
    with db.get_conn() as conn:
        row = conn.execute("SELECT status FROM auth_requests WHERE id = 'r1'").fetchone()
    assert row["status"] == "claimed"


def test_dual_write_raw_read_does_not_expire_stale_request(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET auth_control_plane_mode = 'dual_write' WHERE rowid = 1")
        conn.execute(
            "UPDATE auth_requests SET expires_at = ?, status = 'pending' WHERE id = 'r1'",
            (_ts(3600),),
        )

    response = client.get("/auth-requests")
    assert response.status_code == 200
    with db.get_conn() as conn:
        row = conn.execute("SELECT status FROM auth_requests WHERE id = 'r1'").fetchone()
    assert row["status"] == "pending"


def test_internal_create_is_project_scoped(client: TestClient) -> None:
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "project-dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist={"other-project"},
        )
    response = client.post(
        "/internal/auth/requests",
        json={"project_id": "p1", "source_fact_ids": ["origin"], "auth_ref": "new-target"},
        headers=_headers("project-dispatcher-token"),
    )
    assert response.status_code == 403


def test_dispatcher_project_allowlist_is_enforced_for_event_claim(client: TestClient) -> None:
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "restricted-dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist=set(),
        )
    response = client.post(
        "/internal/auth/events/claim",
        json={"dispatcher_id": "d1"},
        headers=_headers("restricted-dispatcher-token"),
    )
    assert response.status_code == 204


def test_dispatcher_project_allowlist_is_enforced_for_recovery_and_expiry(client: TestClient) -> None:
    with db.get_conn() as conn:
        provision_auth_credential(
            conn,
            "restricted-dispatcher-token",
            actor_id="dispatcher",
            scopes={"dispatcher.auth.consume"},
            project_allowlist=set(),
        )
        conn.execute(
            "UPDATE auth_events SET state = 'claimed', claim_expires_at = ? WHERE id = 'e1'",
            (_ts(60),),
        )
        conn.execute(
            "UPDATE auth_requests SET expires_at = ? WHERE id = 'r1'",
            (_ts(60),),
        )
    recovered = client.post(
        "/internal/auth/events/recover", json={}, headers=_headers("restricted-dispatcher-token")
    )
    expired = client.post(
        "/internal/auth/requests/expire", json={}, headers=_headers("restricted-dispatcher-token")
    )
    assert recovered.status_code == 200 and recovered.json()["recovered"] == 0
    assert expired.status_code == 200 and expired.json()["expired"] == 0
    with db.get_conn() as conn:
        event = conn.execute("SELECT state FROM auth_events WHERE id = 'e1'").fetchone()
        auth_request = conn.execute("SELECT status FROM auth_requests WHERE id = 'r1'").fetchone()
    assert event["state"] == "claimed"
    assert auth_request["status"] == "pending"


def test_auth_request_creation_uses_server_ttl_and_zero_disables_expiry(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET auth_request_ttl = 60 WHERE rowid = 1")
    response = client.post(
        "/internal/auth/requests",
        json={"project_id": "p1", "source_fact_ids": ["origin"], "auth_ref": "new-target"},
        headers=_headers(),
    )
    assert response.status_code == 201
    with db.get_conn() as conn:
        row = conn.execute("SELECT expires_at, expiry_generation FROM auth_requests WHERE id = ?", (response.json()["id"],)).fetchone()
        assert row["expires_at"] is not None
        conn.execute("UPDATE settings SET auth_request_ttl = 0 WHERE rowid = 1")
        conn.execute(
            "INSERT INTO auth_target_configs (auth_ref, login_url) VALUES ('zero-target', 'https://target.example/login')"
        )
        conn.execute(
            "INSERT INTO auth_target_metadata (auth_ref, role, request_reason) VALUES ('zero-target', 'user', 'authentication_required')"
        )
    response = client.post(
        "/internal/auth/requests",
        json={"project_id": "p1", "source_fact_ids": ["origin"], "auth_ref": "zero-target"},
        headers=_headers(),
    )
    with db.get_conn() as conn:
        row = conn.execute("SELECT expires_at FROM auth_requests WHERE id = ?", (response.json()["id"],)).fetchone()
    assert row["expires_at"] is None


def test_internal_create_rejects_worker_supplied_authority_fields(client: TestClient) -> None:
    response = client.post(
        "/internal/auth/requests",
        json={
            "project_id": "p1",
            "source_fact_ids": ["origin"],
            "auth_ref": "new-target",
            "role": "forged",
            "reason": "secret",
            "login_url": "https://evil.invalid/login",
        },
        headers=_headers(),
    )
    assert response.status_code == 422

    response = client.post(
        "/internal/auth/requests",
        json={"project_id": "p1", "source_fact_ids": ["origin"], "auth_ref": "new-target"},
        headers=_headers(),
    )
    assert response.status_code == 201
    assert response.json()["role"] == "admin"
    assert response.json()["login_url"] == "https://target.example/login"
    assert response.json()["reason"] == "authentication_required"


def test_auth_graph_outbox_and_source_keys_are_durable(client: TestClient) -> None:
    with db.get_conn() as conn:
        outbox = conn.execute("PRAGMA table_info(auth_graph_outbox)").fetchall()
        intents = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
        facts = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
    assert outbox
    assert {row["name"] for row in outbox} >= {
        "event_id", "effect_key", "project_id", "request_id", "intent_source_key", "fact_source_key", "fact_kind", "state"
    }
    assert "source_key" in intents
    assert "source_key" in facts


def test_graph_source_key_rpc_is_dispatcher_only(client: TestClient) -> None:
    body = {
        "project_id": "p1",
        "source_key": "auth:event:e1:intent",
        "source_fact_ids": ["origin"],
        "description": "Verify authenticated session",
    }
    denied = client.post(
        "/internal/auth/graph/intents", json=body,
        headers={"Authorization": "Bearer helper-token", "X-Forwarded-Proto": "https"},
    )
    assert denied.status_code in (401, 403)
    allowed = client.post("/internal/auth/graph/intents", json=body, headers=_headers())
    assert allowed.status_code == 200
    assert allowed.json()["source_key"] == body["source_key"]


def test_login_success_control_validates_manifest_before_graph_effect() -> None:
    class Store:
        def validate_capture(self, *args, **kwargs):
            raise ValueError("capture mismatch")

    class Client:
        def claim_auth_event(self, dispatcher_id: str) -> ApiResult:
            return ApiResult(200, data={
                "id": "event-1", "kind": "login_succeeded", "actor_id": "helper-a",
                "request_status": "waiting_user", "helper_actor_id": "helper-a",
                "project_id": "p1", "request_id": "r1", "auth_ref": "target",
                "capture_generation": 1,
            })

        def apply_auth_event(self, event_id: str, dispatcher_id: str, **kwargs: str) -> ApiResult:
            assert kwargs["operation"] == "begin_verification"
            return ApiResult(200, data={"state": "claimed", "status": "verifying"})

        def create_auth_graph_intent(self, *args, **kwargs):
            return ApiResult(200, data={"id": "i-invalid"})

        def conclude_auth_graph_intent(self, *args, **kwargs):
            return ApiResult(200, data={"intent_id": "i-invalid", "fact_id": "f-invalid"})

        def acknowledge_auth_graph_outbox(self, *args, **kwargs):
            return ApiResult(200, data={})

    control = DispatcherAuthControl(Client(), auth_store=Store())
    result = control.consume_once()
    assert result is not None and result.ok


def test_login_success_control_replays_graph_effects_by_event_source_keys() -> None:
    from cairn.auth.models import AuthVerificationResult
    from cairn.dispatcher.config import AuthTargetConfig

    target = AuthTargetConfig.model_validate({
        "name": "target", "base_url": "https://target.example", "login_url": "https://target.example/login",
        "role": "user", "verify": {"url": "https://target.example/private"},
    })

    class Store:
        def validate_capture(self, *args, **kwargs):
            return object()

        def load_state(self, *args):
            return {"cookies": []}

    class Config:
        verify_timeout = 30

        @staticmethod
        def target(name):
            assert name == "target"
            return target

    class Verifier:
        def verify_storage_state(self, _target, _state):
            return AuthVerificationResult(True, True, True, True)

    class Client:
        def __init__(self):
            self.calls = []

        def claim_auth_event(self, dispatcher_id):
            return ApiResult(200, data={
                "id": "event-2", "kind": "login_succeeded", "actor_id": "helper-a",
                "request_status": "waiting_user", "helper_actor_id": "helper-a",
                "project_id": "p1", "request_id": "r1", "auth_ref": "target",
                "capture_generation": 1, "source_fact_ids": ["origin"],
            })

        def apply_auth_event(self, event_id, dispatcher_id, **kwargs):
            self.calls.append(("apply", kwargs))
            return ApiResult(200, data={"state": "claimed", "status": "verifying"})

        def create_auth_graph_intent(self, *args, **kwargs):
            self.calls.append(("intent", args, kwargs))
            return ApiResult(200, data={"id": "i-auth"})

        def conclude_auth_graph_intent(self, *args, **kwargs):
            self.calls.append(("conclude", args, kwargs))
            return ApiResult(200, data={"intent_id": "i-auth", "fact_id": "f-auth"})

        def acknowledge_auth_graph_outbox(self, *args, **kwargs):
            self.calls.append(("ack", args, kwargs))
            return ApiResult(200, data={})

    client = Client()
    result = DispatcherAuthControl(client, auth_store=Store(), auth_config=Config(), verifier=Verifier()).consume_once()
    assert result is not None and result.ok
    assert [call[0] for call in client.calls] == ["apply", "intent", "conclude", "ack"]
    assert client.calls[-1][2]["state"] == "fact_created"


def test_login_failed_control_publishes_one_sanitized_invalid_fact() -> None:
    from cairn.auth.models import AuthVerificationResult

    class Target:
        name = "target"
        role = "user"
        base_url = "https://target.example"

    class Config:
        helper_actor_id = "helper-a"

        @staticmethod
        def target(name):
            return Target()

    class Client:
        def __init__(self):
            self.calls = []

        def claim_auth_event(self, dispatcher_id):
            return ApiResult(200, data={
                "id": "event-fail", "kind": "login_failed", "actor_id": "helper-a",
                "request_status": "waiting_user", "helper_actor_id": "helper-a",
                "project_id": "p1", "request_id": "r1", "auth_ref": "target",
                "source_fact_ids": ["origin"],
            })

        def apply_auth_event(self, event_id, dispatcher_id, **kwargs):
            self.calls.append(("apply", kwargs))
            return ApiResult(200, data={"state": "claimed", "status": "verifying"})

        def create_auth_graph_intent(self, *args, **kwargs):
            self.calls.append(("intent", args, kwargs))
            return ApiResult(200, data={"id": "i-invalid"})

        def conclude_auth_graph_intent(self, *args, **kwargs):
            self.calls.append(("conclude", args, kwargs))
            return ApiResult(200, data={"intent_id": "i-invalid", "fact_id": "f-invalid"})

        def acknowledge_auth_graph_outbox(self, *args, **kwargs):
            self.calls.append(("ack", args, kwargs))
            return ApiResult(200, data={})

    client = Client()
    result = DispatcherAuthControl(client, auth_config=Config(), verifier=object(), auth_store=object()).consume_once()
    assert result is not None and result.ok
    assert [call[0] for call in client.calls] == ["apply", "intent", "conclude", "ack"]
    assert client.calls[-1][2] == {
        "state": "fact_created", "intent_id": "i-invalid", "fact_id": "f-invalid",
        "outcome_code": "login_failed",
    }


def test_invalid_graph_ack_fails_request_without_marking_it_verified(client: TestClient) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE auth_requests SET status = 'verifying', helper_actor_id = 'helper-a' WHERE id = 'r1'")
        conn.execute(
            "INSERT INTO auth_events (id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key, occurred_at, received_at, state, claimed_by, claim_expires_at) VALUES ('e-invalid', 'p1', 'r1', 'target', 'login_failed', 'helper-a', ?, ?, ?, 'claimed', 'd1', ?)",
            (str(uuid4()), _ts(), _ts(), _ts(-60)),
        )
        from cairn.server.services import ensure_auth_graph_outbox
        ensure_auth_graph_outbox(conn, conn.execute("SELECT * FROM auth_events WHERE id = 'e-invalid'").fetchone())
    intent_key = "auth-event:e-invalid:intent"
    fact_key = "auth-event:e-invalid:fact"
    created = client.post("/internal/auth/graph/intents", json={
        "project_id": "p1", "source_key": intent_key, "source_fact_ids": ["origin"],
        "description": "Verify authenticated session",
    }, headers=_headers())
    assert created.status_code == 200
    concluded = client.post("/internal/auth/graph/conclude", json={
        "project_id": "p1", "intent_source_key": intent_key, "fact_source_key": fact_key,
        "description": "AuthSessionInvalid\ntarget=target;\nrole=user;\nevidence=authentication check failed",
    }, headers=_headers())
    assert concluded.status_code == 200
    acknowledged = client.post("/internal/auth/graph/outbox/ack", json={
        "event_id": "e-invalid", "dispatcher_id": "d1", "state": "fact_created",
        "intent_id": concluded.json()["intent_id"], "fact_id": concluded.json()["fact_id"],
        "outcome_code": "login_failed",
    }, headers=_headers())
    assert acknowledged.status_code == 200
    with db.get_conn() as conn:
        event = conn.execute("SELECT state, outcome_code FROM auth_events WHERE id = 'e-invalid'").fetchone()
        request = conn.execute("SELECT status, failure_reason FROM auth_requests WHERE id = 'r1'").fetchone()
        outbox = conn.execute("SELECT state, outcome_code FROM auth_graph_outbox WHERE event_id = 'e-invalid'").fetchone()
    assert dict(event) == {"state": "rejected", "outcome_code": "login_failed"}
    assert dict(request) == {"status": "failed", "failure_reason": "login_failed"}
    assert dict(outbox) == {"state": "fact_created", "outcome_code": "login_failed"}


def test_dispatch_config_accepts_control_plane_mode_and_zero_ttl() -> None:
    from cairn.dispatcher.config import DispatchConfig

    config = DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "auth_control_plane_mode": "dual_write",
            "runtime": {"interval": 60, "max_workers": 2, "max_running_projects": 1, "max_project_workers": 2, "healthcheck_timeout": 5, "prompt_group": "default"},
            "tasks": {"bootstrap": {"timeout": 10, "conclude_timeout": 5}, "reason": {"timeout": 10, "max_intents": 3}, "explore": {"timeout": 10, "conclude_timeout": 5}},
            "container": {"image": "test", "network_mode": "host", "completed_action": "stop"},
            "workers": [{"name": "w", "type": "mock", "task_types": ["reason"], "max_running": 1, "priority": 0}],
        }
    )
    assert config.auth_control_plane_mode == "dual_write"
