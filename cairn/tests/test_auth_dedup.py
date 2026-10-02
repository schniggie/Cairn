from __future__ import annotations

from cairn.server import db
from cairn.server.services import (
    claim_auth_request_atomic,
    find_active_auth_request,
    next_auth_request_id,
)


def _seed_project(conn) -> None:
    conn.execute(
        "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) "
        "VALUES ('proj_001', 't', 'active', 1, '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES ('origin', 'proj_001', 'start')"
    )


def _seed_auth_request(conn, request_id: str = "auth_001", auth_ref: str = "target-user", status: str = "pending") -> None:
    conn.execute(
        """
        INSERT INTO auth_requests (
            id, project_id, source_fact_ids, auth_ref, role, login_url,
            reason, status, created_at
        ) VALUES (?, 'proj_001', 'origin', ?, 'user', NULL, 'need auth', ?, '2026-01-01T00:00:00Z')
        """,
        (request_id, auth_ref, status),
    )


def test_next_auth_request_id_increments(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        assert next_auth_request_id(conn) == "auth_001"
        assert next_auth_request_id(conn) == "auth_002"


def test_find_active_auth_request_matches_active_statuses(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        _seed_project(conn)
        _seed_auth_request(conn, "auth_001", "target-user", "pending")
        _seed_auth_request(conn, "auth_002", "target-user", "completed")
        _seed_auth_request(conn, "auth_003", "other-target", "verifying")

        # Active match returns the in-flight request.
        active = find_active_auth_request(conn, "proj_001", "target-user")
        assert active is not None and active["id"] == "auth_001"

        # Completed requests are not "active", so a different target with active status matches.
        active_other = find_active_auth_request(conn, "proj_001", "other-target")
        assert active_other is not None and active_other["id"] == "auth_003"

        # A completed-only auth_ref has no active request.
        conn.execute(
            "UPDATE auth_requests SET status = 'completed' WHERE id = 'auth_001'"
        )
        assert find_active_auth_request(conn, "proj_001", "target-user") is None


def test_claim_atomic_returns_false_when_not_pending(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        _seed_project(conn)
        _seed_auth_request(conn, "auth_001", "target-user", "claimed")
        assert claim_auth_request_atomic(conn, "auth_001", "helper-a") is False


def test_claim_atomic_succeeds_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        _seed_project(conn)
        _seed_auth_request(conn, "auth_001", "target-user", "pending")
        assert claim_auth_request_atomic(conn, "auth_001", "helper-a") is True
        # Second claim fails because it is no longer pending.
        assert claim_auth_request_atomic(conn, "auth_001", "helper-b") is False
