from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from cairn.server import db
from cairn.server.services import (
    claim_auth_request_atomic,
    expire_stale_claims,
    expire_stale_requests,
    find_active_auth_request,
    get_auth_claim_ttl,
    get_auth_request_ttl,
    utcnow,
)


def _ts(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest.fixture
def conn(tmp_path):
    db._db_path = None
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) "
            "VALUES ('p1', 't', 'active', 1, ?)",
            (_ts(10000),),
        )
        yield conn


def _insert(conn, rid: str, auth_ref: str, status: str, created_ago: int, claimed_ago: int | None) -> None:
    claimed_by = f"helper-{rid}" if status == "claimed" else None
    claimed_at = _ts(claimed_ago) if claimed_ago is not None else None
    conn.execute(
        """
        INSERT INTO auth_requests
            (id, project_id, source_fact_ids, auth_ref, role, reason, status,
             created_at, claimed_by, claimed_at)
        VALUES (?, 'p1', '', ?, 'user', 'need auth', ?, ?, ?, ?)
        """,
        (rid, auth_ref, status, _ts(created_ago), claimed_by, claimed_at),
    )


def test_settings_defaults_expose_ttl(conn) -> None:
    assert get_auth_claim_ttl(conn) == 300
    assert get_auth_request_ttl(conn) == 1800


def test_expire_stale_claims_releases_only_stale(conn) -> None:
    _insert(conn, "r_stale", "t1", "claimed", created_ago=600, claimed_ago=600)
    _insert(conn, "r_fresh", "t1", "claimed", created_ago=10, claimed_ago=10)

    released = expire_stale_claims(conn, 300)

    assert released == 1
    row = conn.execute("SELECT status, claimed_by FROM auth_requests WHERE id='r_stale'").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    row2 = conn.execute("SELECT status FROM auth_requests WHERE id='r_fresh'").fetchone()
    assert row2["status"] == "claimed"


def test_expire_stale_claims_ignores_other_statuses(conn) -> None:
    _insert(conn, "r_waiting", "t1", "waiting_user", created_ago=600, claimed_ago=None)
    _insert(conn, "r_verifying", "t1", "verifying", created_ago=600, claimed_ago=None)

    released = expire_stale_claims(conn, 300)

    assert released == 0
    assert conn.execute("SELECT status FROM auth_requests WHERE id='r_waiting'").fetchone()["status"] == "waiting_user"
    assert conn.execute("SELECT status FROM auth_requests WHERE id='r_verifying'").fetchone()["status"] == "verifying"


def test_expire_stale_requests_marks_active_as_expired(conn) -> None:
    _insert(conn, "r_old_pending", "t1", "pending", created_ago=3000, claimed_ago=None)
    _insert(conn, "r_old_claimed", "t2", "claimed", created_ago=3000, claimed_ago=3000)
    _insert(conn, "r_new", "t3", "pending", created_ago=10, claimed_ago=None)

    expired = expire_stale_requests(conn, 1800)

    assert expired == 2
    assert conn.execute("SELECT status FROM auth_requests WHERE id='r_old_pending'").fetchone()["status"] == "expired"
    assert conn.execute("SELECT status FROM auth_requests WHERE id='r_old_claimed'").fetchone()["status"] == "expired"
    assert conn.execute("SELECT status FROM auth_requests WHERE id='r_new'").fetchone()["status"] == "pending"


def test_reclaimed_claim_can_be_reclaimed_again(conn) -> None:
    """After a stale claim is released, the dedup guard no longer blocks a new claim."""
    _insert(conn, "r1", "t1", "claimed", created_ago=600, claimed_ago=600)

    # Before reclaim, dedup finds it as active.
    assert find_active_auth_request(conn, "p1", "t1") is not None

    expire_stale_claims(conn, 300)

    row = conn.execute("SELECT status FROM auth_requests WHERE id='r1'").fetchone()
    assert row["status"] == "pending"

    # A fresh helper can now atomically claim it.
    assert claim_auth_request_atomic(conn, "r1", "helper-new") is True
    row = conn.execute("SELECT status, claimed_by FROM auth_requests WHERE id='r1'").fetchone()
    assert row["status"] == "claimed"
    assert row["claimed_by"] == "helper-new"


def test_expired_request_no_longer_blocks_dedup(conn) -> None:
    _insert(conn, "r1", "t1", "pending", created_ago=3000, claimed_ago=None)

    expire_stale_requests(conn, 1800)

    # Expired is not in the active status set, so a new request can be created.
    assert find_active_auth_request(conn, "p1", "t1") is None


def test_zero_ttl_disables_reaping(conn) -> None:
    _insert(conn, "r_stale", "t1", "claimed", created_ago=600, claimed_ago=600)
    _insert(conn, "r_old", "t2", "pending", created_ago=3000, claimed_ago=None)

    assert expire_stale_claims(conn, 0) == 0
    assert expire_stale_requests(conn, 0) == 0
