from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from cairn.server.db import get_conn
from cairn.server.models import AuthEvent, CreateAuthEvent
from cairn.server.services import (
    auth_event_to_model,
    get_auth_request_or_404,
    get_project_or_404,
    require_auth_principal,
    utcnow,
)

router = APIRouter(tags=["auth-events"])


@router.post("/auth-events", response_model=AuthEvent, status_code=201)
def create_auth_event(body: CreateAuthEvent, request: Request, response: Response) -> AuthEvent:
    with get_conn() as conn:
        principal = require_auth_principal(
            request,
            conn,
            scope="helper.event.submit",
            project_id=body.project_id,
        )
        get_project_or_404(conn, body.project_id)
        auth_request = get_auth_request_or_404(conn, body.request_id)
        if auth_request["project_id"] != body.project_id or auth_request["auth_ref"] != body.auth_ref:
            raise HTTPException(409, "Event does not match auth request")
        if body.kind == "login_succeeded" and body.capture_generation is None:
            raise HTTPException(422, "capture_generation is required for login_succeeded")

        idempotency_key = str(body.idempotency_key)
        existing = conn.execute(
            "SELECT * FROM auth_events WHERE actor_id = ? AND idempotency_key = ?",
            (principal.actor_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            immutable = (
                existing["project_id"], existing["request_id"], existing["auth_ref"], existing["kind"],
                existing["capture_generation"],
            )
            requested = (
                body.project_id, body.request_id, body.auth_ref, body.kind,
                body.capture_generation,
            )
            if immutable != requested:
                raise HTTPException(409, "Idempotency key conflict")
            response.status_code = 200
            return auth_event_to_model(existing)

        event_id = f"evt_{__import__('uuid').uuid4().hex}"
        now = utcnow()
        conn.execute(
            """
            INSERT INTO auth_events
                (id, project_id, request_id, auth_ref, kind, actor_id, idempotency_key,
                 occurred_at, received_at, state, attempt_count, capture_generation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?)
            """,
            (
                event_id, body.project_id, body.request_id, body.auth_ref, body.kind,
                principal.actor_id, idempotency_key, body.occurred_at, now, body.capture_generation,
            ),
        )
        row = conn.execute("SELECT * FROM auth_events WHERE id = ?", (event_id,)).fetchone()
        assert row is not None
        return auth_event_to_model(row)
