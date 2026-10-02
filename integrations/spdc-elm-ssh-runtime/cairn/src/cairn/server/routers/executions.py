from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    AppendExecutionEventsRequest,
    Artifact,
    ClaimHealthcheckExecutionsRequest,
    ClaimQuestionExecutionsRequest,
    ConcludeResponse,
    CreateExecutionRequest,
    ExecutionConclusionReportRequest,
    ExecutionEventsResponse,
    ExecutionRun,
    FinishExecutionRequest,
    LeaseExecutionRequest,
    PatchExecutionRequest,
    UploadExecutionArtifactRequest,
)
from cairn.server.services import (
    TERMINAL_EXECUTION_STATUSES,
    append_execution_events,
    claim_pending_healthcheck_executions,
    claim_pending_question_executions,
    create_execution_run,
    expire_workers,
    finish_execution,
    get_execution_or_404,
    lease_execution,
    list_execution_events,
    patch_execution,
    submit_execution_conclusion_report,
    upload_execution_artifact,
)

LOG = logging.getLogger(__name__)

router = APIRouter(tags=["executions"])


@router.post("/projects/{project_id}/executions", response_model=ExecutionRun, status_code=201)
def create_project_execution(project_id: str, body: CreateExecutionRequest):
    with get_conn() as conn:
        return create_execution_run(conn, project_id, body)


@router.get("/projects/{project_id}/executions/{execution_id}", response_model=ExecutionRun)
def get_project_execution(project_id: str, execution_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        return get_execution_or_404(conn, project_id, execution_id)


@router.get("/projects/{project_id}/executions/{execution_id}/events", response_model=ExecutionEventsResponse)
def get_project_execution_events(
    project_id: str,
    execution_id: str,
    after_cursor: str | None = None,
    limit: int = 200,
):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        events = list_execution_events(
            conn,
            project_id,
            execution_id,
            after_cursor=after_cursor,
            limit=limit,
        )
    return ExecutionEventsResponse(events=events, next_cursor=events[-1].cursor if events else after_cursor)


@router.post("/dispatcher/executions/lease", response_model=ExecutionRun)
def dispatcher_lease_pending_execution(body: LeaseExecutionRequest):
    with get_conn() as conn:
        return lease_execution(conn, body)


@router.post("/dispatcher/healthcheck-executions/claim", response_model=list[ExecutionRun])
def dispatcher_claim_healthcheck_executions(body: ClaimHealthcheckExecutionsRequest):
    with get_conn() as conn:
        return claim_pending_healthcheck_executions(
            conn,
            dispatcher_id=body.dispatcher_id,
            worker_names=body.worker_names,
            environment_ids=body.environment_ids,
            limit=body.limit,
            lease_seconds=body.lease_seconds,
        )


@router.post("/dispatcher/question-executions/claim", response_model=list[ExecutionRun])
def dispatcher_claim_question_executions(body: ClaimQuestionExecutionsRequest):
    with get_conn() as conn:
        expire_workers(conn)
        return claim_pending_question_executions(
            conn,
            dispatcher_id=body.dispatcher_id,
            worker_names=body.worker_names,
            environment_ids=body.environment_ids,
            limit=body.limit,
            lease_seconds=body.lease_seconds,
        )


@router.post("/dispatcher/intents/{intent_id}/lease-execution", response_model=ExecutionRun, status_code=201)
def dispatcher_lease_intent_execution(intent_id: str, body: LeaseExecutionRequest):
    with get_conn() as conn:
        return lease_execution(conn, body, intent_id=intent_id)


@router.patch("/dispatcher/executions/{execution_id}", response_model=ExecutionRun)
def dispatcher_patch_execution(execution_id: str, body: PatchExecutionRequest):
    with get_conn() as conn:
        execution = get_execution_or_404(conn, None, execution_id)
        _enforce_dispatcher_writer_guard(execution, body.dispatcher_id, body.sink_token, operation="patch")
        return patch_execution(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/events")
def dispatcher_append_execution_events(execution_id: str, body: AppendExecutionEventsRequest):
    with get_conn() as conn:
        execution = get_execution_or_404(conn, None, execution_id)
        _enforce_dispatcher_append_guard(conn, execution, body)
        return append_execution_events(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/finish", response_model=ExecutionRun)
def dispatcher_finish_execution(execution_id: str, body: FinishExecutionRequest):
    with get_conn() as conn:
        return finish_execution(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/conclusion-report", response_model=ConcludeResponse)
def dispatcher_submit_execution_conclusion_report(execution_id: str, body: ExecutionConclusionReportRequest):
    with get_conn() as conn:
        return submit_execution_conclusion_report(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/artifacts", response_model=Artifact, status_code=201)
def dispatcher_upload_execution_artifact(execution_id: str, body: UploadExecutionArtifactRequest):
    with get_conn() as conn:
        return upload_execution_artifact(conn, execution_id, body)


def _enforce_dispatcher_append_guard(
    conn, execution: ExecutionRun, body: AppendExecutionEventsRequest
) -> None:
    """Enforce v3.7 single-writer contract for dispatcher append.

    Rules:
    - pending execution: reject (dispatcher must lease/claim first)
    - terminal execution: reject new events; allow only full-batch idempotent replay
    - owner mismatch: reject (wrong dispatcher)
    - sink_token mismatch: reject (stale sink from same dispatcher)
    """
    if execution.status == "pending":
        LOG.warning(
            "dispatcher_append_rejected execution_id=%s reason=pending dispatcher_id=%s event_count=%d",
            execution.id, body.dispatcher_id, len(body.events),
        )
        raise HTTPException(409, "Execution is pending; dispatcher must lease before appending events")
    _enforce_dispatcher_writer_guard(execution, body.dispatcher_id, body.sink_token, operation="append")
    if execution.status in TERMINAL_EXECUTION_STATUSES:
        for event in body.events:
            if event.event_key is None:
                LOG.warning(
                    "dispatcher_append_rejected execution_id=%s reason=terminal_new_unkeyed_event dispatcher_id=%s",
                    execution.id, body.dispatcher_id,
                )
                raise HTTPException(409, "Execution is terminal; new events are not allowed")
            existing = conn.execute(
                "SELECT 1 FROM execution_events WHERE execution_id = ? AND event_key = ?",
                (execution.id, event.event_key),
            ).fetchone()
            if existing is None:
                LOG.warning(
                    "dispatcher_append_rejected execution_id=%s reason=terminal_new_event "
                    "event_key=%s dispatcher_id=%s",
                    execution.id, event.event_key, body.dispatcher_id,
                )
                raise HTTPException(409, "Execution is terminal; new events are not allowed")
        return


def _enforce_dispatcher_writer_guard(
    execution: ExecutionRun,
    dispatcher_id: str | None,
    sink_token: str | None,
    *,
    operation: str,
) -> None:
    if execution.leased_by is not None and execution.leased_by != dispatcher_id:
        LOG.warning(
            "dispatcher_%s_rejected execution_id=%s reason=owner_mismatch "
            "leased_by=%s request_dispatcher=%s",
            operation,
            execution.id,
            execution.leased_by,
            dispatcher_id,
        )
        raise HTTPException(403, f"Execution is leased by {execution.leased_by}")
    if execution.sink_token is not None and sink_token != execution.sink_token:
        LOG.warning(
            "dispatcher_%s_rejected execution_id=%s reason=sink_token_mismatch "
            "dispatcher_id=%s",
            operation,
            execution.id,
            dispatcher_id,
        )
        raise HTTPException(409, "Sink token mismatch; stale sink writer rejected")
