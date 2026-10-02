from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from cairn.server.db import get_conn
from cairn.server.models import (
    AuthRequest,
    ClaimAuthRequest,
    CreateAuthRequest,
    FailAuthRequest,
)
from cairn.server.services import (
    auth_request_to_model,
    build_auth_state_resolver,
    check_project_active,
    claim_auth_request_atomic,
    expire_stale_claims,
    expire_stale_requests,
    find_active_auth_request,
    get_auth_claim_ttl,
    get_auth_request_or_404,
    get_auth_request_ttl,
    next_auth_request_id,
    utcnow,
    validate_facts_exist,
    _join_source_fact_ids,
    auth_request_expiry,
    guard_legacy_auth_mutation,
    _append_auth_lifecycle,
    reject_migrated_helper_raw_listing,
    get_auth_control_plane_mode,
)

router = APIRouter(tags=["auth-requests"])


def _reap_expired(conn) -> None:
    """Reclaim stale claims and expire stale requests before serving/claiming.

    This is the only place the server has a live connection on the request path, so
    TTL enforcement is done lazily here rather than by a background job. It is cheap
    (indexed UPDATE) and idempotent.
    """
    if get_auth_control_plane_mode(conn) != "legacy":
        return
    expire_stale_claims(conn, get_auth_claim_ttl(conn))
    expire_stale_requests(conn, get_auth_request_ttl(conn))


@router.post(
    "/projects/{project_id}/auth-requests",
    response_model=AuthRequest,
    status_code=201,
)
def create_auth_request(project_id: str, body: CreateAuthRequest, request: Request):
    with get_conn() as conn:
        guard_legacy_auth_mutation(request, conn)
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.source_fact_ids)

        # Do not create a request if a valid session already exists for this auth_ref.
        state = build_auth_state_resolver(conn).resolve(project_id, body.auth_ref)
        if state == "valid":
            raise HTTPException(
                409,
                f"valid session already exists for auth_ref={body.auth_ref}",
            )

        # Dedup: return the existing in-flight request instead of creating a duplicate.
        existing = find_active_auth_request(conn, project_id, body.auth_ref)
        if existing is not None:
            return auth_request_to_model(existing)

        now = utcnow()
        request_id = next_auth_request_id(conn)
        expires_at = auth_request_expiry(now, get_auth_request_ttl(conn))
        conn.execute(
            """
            INSERT INTO auth_requests (
                id, project_id, source_fact_ids, auth_ref, role, login_url,
                reason, status, created_at, expires_at, expiry_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 1)
            """,
            (
                request_id,
                project_id,
                _join_source_fact_ids(body.source_fact_ids),
                body.auth_ref,
                body.role,
                body.login_url,
                body.reason,
                now,
                expires_at,
            ),
        )
        _append_auth_lifecycle(conn, request_id, f"create:{request_id}", "created", "created", recorded_at=now)
        row = get_auth_request_or_404(conn, request_id)
        return auth_request_to_model(row)


@router.get(
    "/auth-requests",
    response_model=list[AuthRequest],
)
def list_auth_requests(request: Request, status: str | None = Query(default=None)):
    with get_conn() as conn:
        reject_migrated_helper_raw_listing(request, conn)
        _reap_expired(conn)
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM auth_requests WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM auth_requests ORDER BY created_at"
            ).fetchall()
        return [auth_request_to_model(row) for row in rows]


@router.post(
    "/auth-requests/{request_id}/claim",
    response_model=AuthRequest,
)
def claim_auth_request(request_id: str, body: ClaimAuthRequest, request: Request):
    with get_conn() as conn:
        guard_legacy_auth_mutation(request, conn)
        _reap_expired(conn)
        get_auth_request_or_404(conn, request_id)
        claimed = claim_auth_request_atomic(conn, request_id, body.helper_id)
        if not claimed:
            raise HTTPException(409, "Auth request already claimed")
        row = get_auth_request_or_404(conn, request_id)
        return auth_request_to_model(row)


def _transition(
    conn, request_id: str, current_status: str, new_status: str, **updates
) -> AuthRequest:
    row = get_auth_request_or_404(conn, request_id)
    if row["status"] != current_status:
        raise HTTPException(
            409,
            f"Auth request is {row['status']}, expected {current_status}",
        )
    now = utcnow()
    sets = ["status = ?"]
    params: list = [new_status]
    for column, value in updates.items():
        sets.append(f"{column} = ?")
        params.append(value)
    if new_status in ("completed", "failed", "cancelled", "expired"):
        sets.append("completed_at = ?")
        params.append(now)
    params.append(request_id)
    conn.execute(
        f"UPDATE auth_requests SET {', '.join(sets)} WHERE id = ?",
        tuple(params),
    )
    return auth_request_to_model(get_auth_request_or_404(conn, request_id))


@router.post(
    "/auth-requests/{request_id}/waiting",
    response_model=AuthRequest,
)
def waiting_user(request_id: str, request: Request):
    with get_conn() as conn:
        guard_legacy_auth_mutation(request, conn)
        return _transition(conn, request_id, "claimed", "waiting_user")


@router.post(
    "/auth-requests/{request_id}/verifying",
    response_model=AuthRequest,
)
def verifying(request_id: str, request: Request):
    with get_conn() as conn:
        guard_legacy_auth_mutation(request, conn)
        return _transition(conn, request_id, "waiting_user", "verifying")


@router.post(
    "/auth-requests/{request_id}/complete",
    response_model=AuthRequest,
)
def complete(request_id: str, request: Request):
    with get_conn() as conn:
        guard_legacy_auth_mutation(request, conn)
        return _transition(conn, request_id, "verifying", "completed")


@router.post(
    "/auth-requests/{request_id}/fail",
    response_model=AuthRequest,
)
def fail(request_id: str, body: FailAuthRequest, request: Request):
    with get_conn() as conn:
        guard_legacy_auth_mutation(request, conn)
        row = get_auth_request_or_404(conn, request_id)
        if row["status"] not in ("claimed", "waiting_user", "verifying"):
            raise HTTPException(
                409,
                f"Auth request is {row['status']}, cannot fail",
            )
        return _transition(
            conn,
            request_id,
            row["status"],
            "failed",
            failure_reason=body.failure_reason,
        )
