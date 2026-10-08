from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from cairn.server.db import get_conn
from cairn.server.models import (
    AuthEvent,
    AuthEventApplyRequest,
    AuthEventClaimRequest,
    AuthRequest,
    CreateAuthRequestInternal,
)
from cairn.server.services import (
    _append_auth_lifecycle,
    apply_auth_event_atomic,
    auth_request_expiry,
    auth_request_to_model,
    claim_auth_event_atomic,
    check_project_active,
    expire_due_auth_requests,
    find_active_auth_request,
    get_auth_request_or_404,
    get_auth_request_ttl,
    next_auth_request_id,
    recover_auth_event_claims,
    require_auth_principal,
    utcnow,
    validate_facts_exist,
    _join_source_fact_ids,
    build_auth_state_resolver,
    intent_to_model,
    _split_source_fact_ids,
    create_auth_graph_intent,
    conclude_auth_graph_intent,
    ack_auth_graph_outbox,
)
from cairn.server.models import (
    AuthGraphIntentRequest,
    AuthGraphConcludeRequest,
    AuthGraphOutboxAckRequest,
)

router = APIRouter(tags=["auth-control"])


def _dispatcher(request: Request, conn):
    principal = require_auth_principal(request, conn, scope="dispatcher.auth.consume")
    if principal.actor_id != "dispatcher" or principal.scopes != frozenset({"dispatcher.auth.consume"}):
        raise HTTPException(403, "Forbidden")
    return principal


@router.post("/internal/auth/events/claim", response_model=None)
@router.post("/internal/auth-events/claim", response_model=None, include_in_schema=False)
def claim_event(body: AuthEventClaimRequest, request: Request) -> dict[str, object] | Response:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        principal = _dispatcher(request, conn)
        row = claim_auth_event_atomic(conn, body.dispatcher_id, project_allowlist=principal.project_allowlist)
        if row is None:
            return Response(status_code=204)
        result = AuthEvent.model_validate(dict(row)).model_dump(mode="json")
        auth_request = get_auth_request_or_404(conn, row["request_id"])
        # Dispatcher needs the current persisted status to select the legal
        # operation; this is an internal-only claim response field.
        result["request_status"] = auth_request["status"]
        result["helper_actor_id"] = auth_request["helper_actor_id"]
        verifying = conn.execute(
            """
            SELECT event_id FROM auth_lifecycle_events
            WHERE request_id = ? AND kind = 'verifying'
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (row["request_id"],),
        ).fetchone()
        result["verification_event_id"] = verifying["event_id"] if verifying is not None else None
        result["source_fact_ids"] = _split_source_fact_ids(auth_request["source_fact_ids"])
        outbox = conn.execute("SELECT * FROM auth_graph_outbox WHERE event_id = ?", (row["id"],)).fetchone()
        result["graph_outbox"] = dict(outbox) if outbox is not None else None
        return result


@router.post("/internal/auth/events/recover")
@router.post("/internal/auth-events/recover", include_in_schema=False)
def recover_events(request: Request) -> dict[str, int]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        principal = _dispatcher(request, conn)
        return {"recovered": recover_auth_event_claims(conn, project_allowlist=principal.project_allowlist)}


@router.post("/internal/auth/requests/expire")
@router.post("/internal/auth-requests/expire", include_in_schema=False)
def expire_requests(request: Request) -> dict[str, int]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        principal = _dispatcher(request, conn)
        return {"expired": expire_due_auth_requests(conn, project_allowlist=principal.project_allowlist)}


@router.post("/internal/auth/events/apply")
@router.post("/internal/auth-events/apply", include_in_schema=False)
def apply_event(body: AuthEventApplyRequest, request: Request) -> dict[str, object]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        principal = _dispatcher(request, conn)
        event_row = conn.execute("SELECT project_id FROM auth_events WHERE id = ?", (body.event_id,)).fetchone()
        if event_row is not None and "*" not in principal.project_allowlist and event_row["project_id"] not in principal.project_allowlist:
            raise HTTPException(403, "Forbidden")
        event, auth_request = apply_auth_event_atomic(
            conn,
            body.event_id,
            body.dispatcher_id,
            operation=body.operation,
            outcome_code=body.outcome_code,
        )
        event_model = AuthEvent.model_validate(dict(event))
        request_model = auth_request_to_model(auth_request)
        result = request_model.model_dump()
        result["event"] = event_model.model_dump(mode="json")
        result["state"] = event_model.state
        return result


@router.post("/internal/auth/requests", response_model=AuthRequest, status_code=201)
@router.post("/internal/auth-requests", response_model=AuthRequest, status_code=201, include_in_schema=False)
def create_request(body: CreateAuthRequestInternal, request: Request) -> AuthRequest:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        principal = _dispatcher(request, conn)
        if "*" not in principal.project_allowlist and body.project_id not in principal.project_allowlist:
            raise HTTPException(403, "Forbidden")
        target = conn.execute(
            "SELECT c.login_url, m.role, m.request_reason "
            "FROM auth_target_configs c JOIN auth_target_metadata m ON m.auth_ref = c.auth_ref "
            "WHERE c.auth_ref = ?",
            (body.auth_ref,),
        ).fetchone()
        if target is None:
            raise HTTPException(422, "Unknown auth target")
        check_project_active(conn, body.project_id)
        validate_facts_exist(conn, body.project_id, body.source_fact_ids)
        if build_auth_state_resolver(conn).resolve(body.project_id, body.auth_ref) == "valid":
            raise HTTPException(409, "valid session already exists")
        existing = find_active_auth_request(conn, body.project_id, body.auth_ref)
        if existing is not None:
            return auth_request_to_model(existing)
        now = utcnow()
        request_id = next_auth_request_id(conn)
        expires_at = auth_request_expiry(now, get_auth_request_ttl(conn))
        conn.execute(
            """
            INSERT INTO auth_requests
                (id, project_id, source_fact_ids, auth_ref, role, login_url, reason,
                 status, created_at, expires_at, expiry_generation)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 1)
            """,
            (
                request_id,
                body.project_id,
                _join_source_fact_ids(body.source_fact_ids),
                body.auth_ref,
                target["role"],
                target["login_url"],
                target["request_reason"],
                now,
                expires_at,
            ),
        )
        _append_auth_lifecycle(conn, request_id, f"create:{request_id}", "created", "created", recorded_at=now)
        return auth_request_to_model(get_auth_request_or_404(conn, request_id))


@router.post("/internal/auth/graph/intents", include_in_schema=False)
@router.post("/internal/auth-graph/intents", include_in_schema=False)
def create_graph_intent(body: AuthGraphIntentRequest, request: Request) -> dict[str, object]:
    with get_conn() as conn:
        principal = _dispatcher(request, conn)
        if "*" not in principal.project_allowlist and body.project_id not in principal.project_allowlist:
            raise HTTPException(403, "Forbidden")
        return create_auth_graph_intent(
            conn,
            project_id=body.project_id,
            source_key=body.source_key,
            source_fact_ids=body.source_fact_ids,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
        )


@router.get("/internal/auth/graph/intents/{project_id}", include_in_schema=False)
@router.get("/internal/auth-graph/intents/{project_id}", include_in_schema=False)
def lookup_graph_intent(project_id: str, request: Request, source_key: str = Query(...)) -> dict[str, object]:
    with get_conn() as conn:
        principal = _dispatcher(request, conn)
        if "*" not in principal.project_allowlist and project_id not in principal.project_allowlist:
            raise HTTPException(403, "Forbidden")
        row = conn.execute(
            "SELECT * FROM intents WHERE project_id = ? AND source_key = ?", (project_id, source_key)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Auth graph intent not found")
        result = intent_to_model(conn, row, project_id).model_dump(by_alias=True)
        result["source_key"] = source_key
        return result


@router.post("/internal/auth/graph/conclude", include_in_schema=False)
@router.post("/internal/auth-graph/conclude", include_in_schema=False)
def conclude_graph_intent(body: AuthGraphConcludeRequest, request: Request) -> dict[str, object]:
    with get_conn() as conn:
        principal = _dispatcher(request, conn)
        if "*" not in principal.project_allowlist and body.project_id not in principal.project_allowlist:
            raise HTTPException(403, "Forbidden")
        return conclude_auth_graph_intent(
            conn,
            project_id=body.project_id,
            intent_source_key=body.intent_source_key,
            fact_source_key=body.fact_source_key,
            worker=body.worker,
            description=body.description,
        )


@router.post("/internal/auth/graph/outbox/ack", include_in_schema=False)
@router.post("/internal/auth-graph/outbox/ack", include_in_schema=False)
def acknowledge_graph_outbox(body: AuthGraphOutboxAckRequest, request: Request) -> dict[str, object]:
    with get_conn() as conn:
        principal = _dispatcher(request, conn)
        event = conn.execute("SELECT project_id FROM auth_events WHERE id = ?", (body.event_id,)).fetchone()
        if event is None:
            raise HTTPException(404, "Auth event not found")
        if "*" not in principal.project_allowlist and event["project_id"] not in principal.project_allowlist:
            raise HTTPException(403, "Forbidden")
        event_row, request_row, outbox = ack_auth_graph_outbox(
            conn, body.event_id, body.dispatcher_id, state=body.state,
            intent_id=body.intent_id, fact_id=body.fact_id,
            outcome_code=body.outcome_code,
        )
        return {"event": dict(event_row), "request": auth_request_to_model(request_row).model_dump(mode="json"), "outbox": dict(outbox)}
