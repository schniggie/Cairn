from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cairn.server.db import get_conn
from cairn.server.models import (
    AppendExecutionEventsRequest,
    CreateExecutionRequest,
    ExecutionEventAppend,
    ExecutionEventsResponse,
)
from cairn.server.services import (
    append_execution_events,
    create_execution_run,
    execution_event_row_to_model,
    expire_workers,
    get_execution_or_404,
    get_project_or_404,
    next_branch_id,
    utcnow,
    loads_json_object,
)

router = APIRouter(tags=["branches"])


class CreateBranchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_kind: Literal["fact", "intent", "execution", "branch"] | None = None
    anchor_id: str | None = None
    mode: Literal["resume", "fork", "fresh_context"]
    worker_name: str | None = None
    source_execution_id: str | None = None

    @field_validator("anchor_id", "worker_name", "source_execution_id")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class BranchResponse(BaseModel):
    id: str
    project_id: str
    source_execution_id: str | None = None
    parent_branch_id: str | None = None
    anchor_kind: str | None = None
    anchor_id: str | None = None
    mode: str
    status: str
    warnings: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class BranchMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    metadata: dict[str, Any] | None = None

    @field_validator("message")
    @classmethod
    def required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


@router.get("/projects/{project_id}/branches", response_model=list[BranchResponse])
def list_branches(project_id: str, anchor_kind: str | None = None, anchor_id: str | None = None, limit: int = 50):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if anchor_kind is not None:
            clauses.append("anchor_kind = ?")
            params.append(anchor_kind)
        if anchor_id is not None:
            clauses.append("anchor_id = ?")
            params.append(anchor_id)
        rows = conn.execute(
            f"""
            SELECT *
            FROM branches
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at, id
            LIMIT ?
            """,
            (*params, max(1, min(limit, 200))),
        ).fetchall()
        return [BranchResponse(**dict(row)) for row in rows]


@router.post("/projects/{project_id}/branches", response_model=BranchResponse, status_code=201)
def create_branch(project_id: str, body: CreateBranchRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        if body.mode in {"resume", "fork"}:
            if body.source_execution_id is None:
                raise HTTPException(400, "fork/resume require source_execution_id")
            source = get_execution_or_404(conn, project_id, body.source_execution_id)
            if source.remote_session_out_status != "available":
                raise HTTPException(409, "Source execution has no available remote session")
            _check_branch_availability(conn, source, body.mode, body.worker_name)
        now = utcnow()
        branch_id = next_branch_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO branches (
                id, project_id, source_execution_id, parent_branch_id,
                anchor_kind, anchor_id, mode, status, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'active', ?, ?)
            """,
            (
                branch_id,
                project_id,
                body.source_execution_id,
                body.anchor_kind,
                body.anchor_id,
                body.mode,
                now,
                now,
            ),
        )
        return _branch_response(conn, project_id, branch_id)


@router.post("/projects/{project_id}/branches/{branch_id}/messages")
def post_branch_message(project_id: str, branch_id: str, body: BranchMessageRequest):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        branch = _branch_row(conn, project_id, branch_id)
        session = _next_session_input(conn, branch)
        session_action = _session_action(conn, branch)
        execution = create_execution_run(
            conn,
            project_id,
            CreateExecutionRequest(
                branch_id=branch_id,
                task_type="question",
                phase="followup",
                session_action=session_action,
                remote_session_in_kind=session["kind"],
                remote_session_in_id=session["id"],
                remote_session_in_status=session["status"],
                input_snapshot={"branch_id": branch_id, "message": body.message},
                metadata=body.metadata,
            ),
        )
        if session_action == "resume_continue":
            _acquire_session_lock(conn, branch, execution.id, session)
        append_execution_events(
            conn,
            execution.id,
            AppendExecutionEventsRequest(
                events=[
                    ExecutionEventAppend(
                        event_type="message",
                        role="user",
                        payload={"text": body.message},
                        event_key=f"{execution.id}:user:1",
                    )
                ]
            ),
        )
        return {"branch": _branch_response(conn, project_id, branch_id), "execution": execution}


@router.get("/projects/{project_id}/branches/{branch_id}/timeline", response_model=ExecutionEventsResponse)
def get_branch_timeline(project_id: str, branch_id: str, after_cursor: str | None = None, limit: int = 200):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        _branch_row(conn, project_id, branch_id)
        after_project_seq = 0
        if after_cursor:
            row = conn.execute(
                """
                SELECT ev.project_seq, er.branch_id
                FROM execution_events ev
                JOIN execution_runs er ON er.id = ev.execution_id
                WHERE ev.project_id = ? AND ev.cursor = ?
                """,
                (project_id, after_cursor),
            ).fetchone()
            if row is None:
                any_cursor = conn.execute("SELECT 1 FROM execution_events WHERE cursor = ? LIMIT 1", (after_cursor,)).fetchone()
                raise HTTPException(400, "Foreign cursor" if any_cursor is not None else "Invalid cursor")
            if row["branch_id"] != branch_id:
                raise HTTPException(400, "Cursor does not belong to branch")
            after_project_seq = row["project_seq"]
        rows = conn.execute(
            """
            SELECT ev.*
            FROM execution_events ev
            JOIN execution_runs er ON er.id = ev.execution_id
            WHERE er.project_id = ?
              AND er.branch_id = ?
              AND ev.project_seq > ?
            ORDER BY ev.project_seq
            LIMIT ?
            """,
            (project_id, branch_id, after_project_seq, max(1, min(limit, 1000))),
        ).fetchall()
    events = [execution_event_row_to_model(row) for row in rows]
    return ExecutionEventsResponse(events=events, next_cursor=events[-1].cursor if events else after_cursor)


def _branch_row(conn, project_id: str, branch_id: str):
    row = conn.execute(
        "SELECT * FROM branches WHERE project_id = ? AND id = ?",
        (project_id, branch_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Branch not found")
    return row


def _branch_response(conn, project_id: str, branch_id: str) -> BranchResponse:
    row = _branch_row(conn, project_id, branch_id)
    return BranchResponse(**dict(row), warnings=_branch_warnings(conn, row))


def _session_action(conn, branch) -> str:
    if branch["mode"] == "fresh_context":
        return "fresh_context"
    if branch["mode"] == "resume":
        return "resume_continue"
    prior = conn.execute(
        """
        SELECT 1 FROM execution_runs
        WHERE project_id = ?
          AND branch_id = ?
          AND status = 'succeeded'
        LIMIT 1
        """,
        (branch["project_id"], branch["id"]),
    ).fetchone()
    return "branch_continue" if prior is not None else "fork_initial"


def _next_session_input(conn, branch) -> dict[str, str | None]:
    latest = conn.execute(
        """
        SELECT *
        FROM execution_runs
        WHERE project_id = ?
          AND branch_id = ?
          AND status = 'succeeded'
          AND remote_session_out_status = 'available'
        ORDER BY finished_at DESC, created_at DESC
        LIMIT 1
        """,
        (branch["project_id"], branch["id"]),
    ).fetchone()
    source = latest
    if source is None and branch["source_execution_id"] is not None:
        source = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (branch["source_execution_id"],)).fetchone()
    if source is None:
        return {"kind": None, "id": None, "status": None}
    return {
        "kind": source["remote_session_out_kind"],
        "id": source["remote_session_out_id"],
        "status": source["remote_session_out_status"],
    }


def _acquire_session_lock(conn, branch, execution_id: str, session: dict[str, str | None]) -> None:
    if not session["kind"] or not session["id"]:
        raise HTTPException(409, "Resume requires an available remote session")
    now = utcnow()
    conn.execute(
        """
        DELETE FROM execution_session_locks
        WHERE project_id = ?
          AND remote_session_kind = ?
          AND remote_session_id = ?
          AND lease_expires_at <= ?
        """,
        (branch["project_id"], session["kind"], session["id"], now),
    )
    existing = conn.execute(
        """
        SELECT execution_id FROM execution_session_locks
        WHERE project_id = ?
          AND remote_session_kind = ?
          AND remote_session_id = ?
        """,
        (branch["project_id"], session["kind"], session["id"]),
    ).fetchone()
    if existing is not None:
        raise HTTPException(409, "Remote session is locked by another execution")
    conn.execute(
        """
        INSERT INTO execution_session_locks (
            project_id, remote_session_kind, remote_session_id,
            execution_id, branch_id, lease_expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            branch["project_id"],
            session["kind"],
            session["id"],
            execution_id,
            branch["id"],
            "2999-01-01T00:00:00Z",
            now,
        ),
    )


def _check_branch_availability(conn, source, mode: str, requested_worker_name: str | None) -> None:
    worker_name = requested_worker_name or source.worker_name
    if worker_name is None:
        raise HTTPException(409, "source_worker_identity_missing")
    worker = conn.execute("SELECT * FROM worker_inventory WHERE name = ?", (worker_name,)).fetchone()
    if worker is None:
        raise HTTPException(409, "source_worker_missing")
    capability = loads_json_object(worker["question_capability_json"]) or {}
    unavailable = capability.get("unavailable_reasons") if isinstance(capability.get("unavailable_reasons"), dict) else {}
    if mode == "fork" and not capability.get("can_fork_session"):
        raise HTTPException(409, unavailable.get("fork") or "fork_unavailable")
    if mode == "resume" and not capability.get("can_resume_session"):
        raise HTTPException(409, unavailable.get("resume") or "resume_unavailable")


def _branch_warnings(conn, branch) -> list[str]:
    if branch["mode"] not in {"fork", "resume"} or branch["source_execution_id"] is None:
        return []
    source = conn.execute(
        "SELECT * FROM execution_runs WHERE project_id = ? AND id = ?",
        (branch["project_id"], branch["source_execution_id"]),
    ).fetchone()
    if source is None:
        return []
    return ["worker_environment_unhealthy"] if _source_identity_unhealthy(conn, source) else []


def _source_identity_unhealthy(conn, source) -> bool:
    environment_id = _source_value(source, "environment_id")
    worker_name = _source_value(source, "worker_name")
    worker_type = _source_value(source, "worker_type")
    endpoint_id = _source_value(source, "endpoint_id") or ""
    model_profile_id = _source_value(source, "model_profile_id") or ""
    if not environment_id or not worker_name or not worker_type:
        return False
    row = conn.execute(
        """
        SELECT *
        FROM worker_runtime_health
        WHERE environment_id = ?
          AND worker_name = ?
          AND worker_type = ?
          AND endpoint_id = ?
          AND model_profile_id = ?
        """,
        (
            environment_id,
            worker_name,
            worker_type,
            endpoint_id,
            model_profile_id,
        ),
    ).fetchone()
    if row is None or row["status"] != "unhealthy":
        return False
    disabled_until = row["disabled_until"]
    return disabled_until is None or disabled_until > utcnow()


def _source_value(source, key: str):
    if hasattr(source, key):
        return getattr(source, key)
    return source[key]
