from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from cairn.server.db import CommittedHTTPException
from cairn.server.models import (
    ConcludeResponse,
    ContinueEvidence,
    ExecutionMode,
    Fact,
    IntentRun,
    IntentRunClaimRequest,
    IntentRunClaimResponse,
    IntentRunConcludeRequest,
    IntentRunFailRequest,
    IntentRunLeaseRequest,
    IntentRunYieldRequest,
    encode_checkpoint_json,
)
from cairn.server.services import (
    check_project_active,
    get_intent_or_404,
    get_intent_timeout,
    intent_to_model,
    next_fact_id,
    utcnow,
)


MAX_CUMULATIVE_EVIDENCE_ITEMS = 256


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("stored checkpoint is not a JSON object")
    return loaded


def _load_evidence(value: str) -> ContinueEvidence:
    return ContinueEvidence.model_validate_json(value)


def _clean_error(error: str) -> str:
    return " ".join(error.split())[:500]


def _retry_at(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_row_to_model(row: sqlite3.Row) -> IntentRun:
    return IntentRun(
        project_id=row["project_id"],
        intent_id=row["intent_id"],
        status=row["status"],
        worker_name=row["worker_name"],
        worker_type=row["worker_type"],
        has_session=row["session_id"] is not None,
        claim_id=row["claim_id"],
        session_id=row["session_id"],
        checkpoint=_load_json_object(row["checkpoint_json"]),
        evidence=_load_evidence(row["evidence_json"]),
        attempt_count=row["attempt_count"],
        no_progress_count=row["no_progress_count"],
        next_retry_at=row["next_retry_at"],
        last_error=row["last_error"],
        started_at=row["started_at"],
        last_started_at=row["last_started_at"],
        last_yielded_at=row["last_yielded_at"],
        completed_at=row["completed_at"],
        updated_at=row["updated_at"],
    )


def _get_run_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intent_runs WHERE project_id = ? AND intent_id = ?",
        (project_id, intent_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent run not found")
    return row


def _execution_mode_for_running(row: sqlite3.Row) -> ExecutionMode:
    if row["session_id"] is not None:
        return "session"
    if row["checkpoint_json"] is not None:
        return "checkpoint_only"
    return "new"


def _claim_response(row: sqlite3.Row, mode: ExecutionMode) -> IntentRunClaimResponse:
    return IntentRunClaimResponse(
        **_run_row_to_model(row).model_dump(mode="python"), execution_mode=mode
    )


def expire_intent_leases(
    conn: sqlite3.Connection, project_id: str | None = None
) -> set[tuple[str, str]]:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        SELECT project_id, id
        FROM intents
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple[object, ...] = (now, timeout)
    if project_id is not None:
        query += " AND project_id = ?"
        params = (now, timeout, project_id)
    rows = conn.execute(query, params).fetchall()
    for row in rows:
        conn.execute(
            """
            UPDATE intent_runs
            SET status = 'yielded', claim_id = NULL, last_yielded_at = ?, updated_at = ?
            WHERE project_id = ? AND intent_id = ? AND status = 'running'
            """,
            (now, now, row["project_id"], row["id"]),
        )
        conn.execute(
            """
            UPDATE intents
            SET worker = NULL, last_heartbeat_at = NULL
            WHERE project_id = ? AND id = ? AND to_fact_id IS NULL
            """,
            (row["project_id"], row["id"]),
        )
    orphan_query = """
        SELECT ir.project_id, ir.intent_id
        FROM intent_runs ir
        JOIN intents i
          ON i.project_id = ir.project_id AND i.id = ir.intent_id
        WHERE ir.status = 'running' AND i.to_fact_id IS NULL AND i.worker IS NULL
    """
    orphan_params: tuple[object, ...] = ()
    if project_id is not None:
        orphan_query += " AND ir.project_id = ?"
        orphan_params = (project_id,)
    orphans = conn.execute(orphan_query, orphan_params).fetchall()
    for row in orphans:
        conn.execute(
            """
            UPDATE intent_runs
            SET status = 'yielded', claim_id = NULL, last_yielded_at = ?, updated_at = ?
            WHERE project_id = ? AND intent_id = ? AND status = 'running'
            """,
            (now, now, row["project_id"], row["intent_id"]),
        )
    return {(row["project_id"], row["id"]) for row in rows} | {
        (row["project_id"], row["intent_id"])
        for row in orphans
    }


def release_project_intent_leases(
    conn: sqlite3.Connection, project_id: str
) -> None:
    now = utcnow()
    conn.execute(
        """
        UPDATE intent_runs
        SET status = 'yielded', claim_id = NULL, last_yielded_at = ?, updated_at = ?
        WHERE project_id = ? AND status = 'running'
          AND EXISTS (
              SELECT 1 FROM intents i
              WHERE i.project_id = intent_runs.project_id
                AND i.id = intent_runs.intent_id
                AND i.to_fact_id IS NULL
          )
        """,
        (now, now, project_id),
    )
    conn.execute(
        """
        UPDATE intents
        SET worker = NULL, last_heartbeat_at = NULL
        WHERE project_id = ? AND to_fact_id IS NULL
        """,
        (project_id,),
    )


def claim_run(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    body: IntentRunClaimRequest,
) -> IntentRunClaimResponse:
    # claim_id 由可信 Dispatcher 为每次新认领生成全新 UUID；服务端不维护历史代际表。
    check_project_active(conn, project_id)
    expire_intent_leases(conn, project_id)
    intent = get_intent_or_404(conn, project_id, intent_id)
    if intent["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    run = conn.execute(
        "SELECT * FROM intent_runs WHERE project_id = ? AND intent_id = ?",
        (project_id, intent_id),
    ).fetchone()

    if intent["worker"] is not None:
        if (
            run is not None
            and run["status"] == "running"
            and intent["worker"] == body.worker
            and run["worker_name"] == body.worker
            and run["claim_id"] == body.claim_id
        ):
            now = utcnow()
            conn.execute(
                "UPDATE intents SET last_heartbeat_at = ? WHERE project_id = ? AND id = ?",
                (now, project_id, intent_id),
            )
            conn.execute(
                "UPDATE intent_runs SET updated_at = ? WHERE project_id = ? AND intent_id = ?",
                (now, project_id, intent_id),
            )
            current = _get_run_or_404(conn, project_id, intent_id)
            mode = current["execution_mode"] or _execution_mode_for_running(current)
            return _claim_response(current, mode)
        raise HTTPException(409, f"Intent is currently claimed by {intent['worker']}")

    now = utcnow()
    if run is None:
        conn.execute(
            """
            INSERT INTO intent_runs (
                project_id, intent_id, status, worker_name, worker_type, claim_id,
                execution_mode,
                evidence_json, attempt_count, no_progress_count,
                started_at, last_started_at, updated_at
            ) VALUES (?, ?, 'running', ?, ?, ?, 'new', '{}', 1, 0, ?, ?, ?)
            """,
            (project_id, intent_id, body.worker, body.worker_type, body.claim_id, now, now, now),
        )
        mode: ExecutionMode = "new"
    else:
        if run["status"] == "completed":
            raise HTTPException(409, "Intent run is completed")
        if run["status"] == "running":
            raise HTTPException(409, "Intent run has an inconsistent running lease")
        if run["status"] == "failed":
            if run["next_retry_at"] is None:
                raise HTTPException(409, "Intent run retry attempts are exhausted")
            if run["next_retry_at"] > now:
                raise HTTPException(409, "Intent run retry is not due yet")
            mode = "checkpoint_only"
        elif (
            run["session_id"] is not None
            and run["worker_type"] == body.worker_type
            and (body.worker_type != "pi" or run["worker_name"] == body.worker)
        ):
            mode = "session"
        elif run["session_id"] is None and run["checkpoint_json"] is None:
            mode = "new"
        else:
            mode = "checkpoint_only"

        new_session = mode != "session"
        conn.execute(
            """
            UPDATE intent_runs
            SET status = 'running', worker_name = ?, worker_type = ?, claim_id = ?,
                execution_mode = ?,
                session_id = CASE WHEN ? THEN NULL ELSE session_id END,
                attempt_count = attempt_count + CASE WHEN ? THEN 1 ELSE 0 END,
                no_progress_count = CASE WHEN ? THEN 0 ELSE no_progress_count END,
                next_retry_at = NULL, last_error = NULL,
                last_started_at = ?, updated_at = ?
            WHERE project_id = ? AND intent_id = ?
            """,
            (
                body.worker,
                body.worker_type,
                body.claim_id,
                mode,
                new_session,
                new_session,
                new_session,
                now,
                now,
                project_id,
                intent_id,
            ),
        )

    conn.execute(
        "UPDATE intents SET worker = ?, last_heartbeat_at = ? "
        "WHERE project_id = ? AND id = ? AND to_fact_id IS NULL",
        (body.worker, now, project_id, intent_id),
    )
    return _claim_response(_get_run_or_404(conn, project_id, intent_id), mode)


def _get_claimed_running_run(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    body: IntentRunLeaseRequest,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    check_project_active(conn, project_id)
    expired = expire_intent_leases(conn, project_id)
    if (project_id, intent_id) in expired:
        raise CommittedHTTPException(409, "Intent run lease expired")
    intent = get_intent_or_404(conn, project_id, intent_id)
    if intent["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    run = _get_run_or_404(conn, project_id, intent_id)
    if run["status"] != "running":
        raise HTTPException(409, f"Intent run is {run['status']}")
    if (
        intent["worker"] != body.worker
        or run["worker_name"] != body.worker
        or run["claim_id"] != body.claim_id
    ):
        raise HTTPException(409, "Intent run claim is held by another execution")
    return intent, run


def heartbeat_run(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    body: IntentRunLeaseRequest,
) -> IntentRun:
    _get_claimed_running_run(conn, project_id, intent_id, body)
    now = utcnow()
    conn.execute(
        "UPDATE intents SET last_heartbeat_at = ? WHERE project_id = ? AND id = ?",
        (now, project_id, intent_id),
    )
    conn.execute(
        "UPDATE intent_runs SET updated_at = ? WHERE project_id = ? AND intent_id = ?",
        (now, project_id, intent_id),
    )
    return _run_row_to_model(_get_run_or_404(conn, project_id, intent_id))


def _checkpoint_json(checkpoint: dict[str, Any] | None) -> str | None:
    if checkpoint is None:
        return None
    try:
        return encode_checkpoint_json(checkpoint)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _merge_evidence(
    existing: ContinueEvidence, incoming: ContinueEvidence
) -> tuple[ContinueEvidence, bool]:
    artifacts = list(existing.artifacts)
    artifact_versions = {(item.path, item.sha256, item.size) for item in artifacts}
    changed = False
    for artifact in incoming.artifacts:
        identity = (artifact.path, artifact.sha256, artifact.size)
        if identity not in artifact_versions:
            artifact_versions.add(identity)
            artifacts.append(artifact)
            changed = True
    if len(artifacts) > MAX_CUMULATIVE_EVIDENCE_ITEMS:
        raise HTTPException(422, "Too many cumulative artifact versions")

    cursors = dict(existing.cursors)
    for key, cursor in incoming.cursors.items():
        previous = cursors.get(key)
        if previous is None or cursor > previous:
            cursors[key] = cursor
            changed = True
    if len(cursors) > MAX_CUMULATIVE_EVIDENCE_ITEMS:
        raise HTTPException(422, "Too many cumulative cursors")

    milestones = list(existing.milestones)
    milestone_ids = {item.id for item in milestones}
    known_artifact_paths = {item.path for item in artifacts}
    for milestone in incoming.milestones:
        if milestone.artifact not in known_artifact_paths:
            raise HTTPException(422, f"Milestone {milestone.id} references an unknown artifact")
        if milestone.id not in milestone_ids:
            milestone_ids.add(milestone.id)
            milestones.append(milestone)
            changed = True
    if len(milestones) > MAX_CUMULATIVE_EVIDENCE_ITEMS:
        raise HTTPException(422, "Too many cumulative milestones")

    return (
        ContinueEvidence(artifacts=artifacts, cursors=cursors, milestones=milestones),
        changed,
    )


def yield_run(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    body: IntentRunYieldRequest,
) -> IntentRun:
    _, run = _get_claimed_running_run(conn, project_id, intent_id, body)
    checkpoint_json = _checkpoint_json(body.checkpoint)
    if checkpoint_json is None:
        checkpoint_json = run["checkpoint_json"]
    session_id = body.session_id if body.session_id is not None else run["session_id"]
    evidence, progressed = _merge_evidence(
        _load_evidence(run["evidence_json"]), body.evidence
    )
    no_progress = 0 if progressed else run["no_progress_count"] + 1
    exhausted = no_progress >= body.max_no_progress_slices
    now = utcnow()
    status = "failed" if exhausted else "yielded"
    error = "No verifiable progress across execution slices" if exhausted else None
    next_retry_at = _retry_at(body.retry_after_seconds) if exhausted else None
    conn.execute(
        """
        UPDATE intent_runs
        SET status = ?, claim_id = NULL, session_id = ?, checkpoint_json = ?,
            evidence_json = ?, no_progress_count = ?, next_retry_at = ?, last_error = ?,
            last_yielded_at = ?, updated_at = ?
        WHERE project_id = ? AND intent_id = ?
        """,
        (
            status,
            session_id,
            checkpoint_json,
            _json_dump(evidence.model_dump(mode="json")),
            no_progress,
            next_retry_at,
            error,
            now,
            now,
            project_id,
            intent_id,
        ),
    )
    conn.execute(
        "UPDATE intents SET worker = NULL, last_heartbeat_at = NULL "
        "WHERE project_id = ? AND id = ?",
        (project_id, intent_id),
    )
    return _run_row_to_model(_get_run_or_404(conn, project_id, intent_id))


def conclude_run(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    body: IntentRunConcludeRequest,
) -> ConcludeResponse:
    _get_claimed_running_run(conn, project_id, intent_id, body)
    now = utcnow()
    fact_id = next_fact_id(conn, project_id)
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
        (fact_id, project_id, body.description),
    )
    conn.execute(
        """
        UPDATE intents
        SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ?
        WHERE project_id = ? AND id = ?
        """,
        (fact_id, body.worker, now, now, project_id, intent_id),
    )
    conn.execute(
        """
        UPDATE intent_runs
        SET status = 'completed', claim_id = NULL,
            session_id = COALESCE(?, session_id), next_retry_at = NULL,
            last_error = NULL, completed_at = ?, updated_at = ?
        WHERE project_id = ? AND intent_id = ?
        """,
        (body.session_id, now, now, project_id, intent_id),
    )
    intent = get_intent_or_404(conn, project_id, intent_id)
    return ConcludeResponse(
        fact=Fact(id=fact_id, description=body.description),
        intent=intent_to_model(conn, intent, project_id),
    )


def fail_run(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    body: IntentRunFailRequest,
) -> IntentRun:
    _, run = _get_claimed_running_run(conn, project_id, intent_id, body)
    now = utcnow()
    session_id = body.session_id if body.session_id is not None else run["session_id"]
    conn.execute(
        """
        UPDATE intent_runs
        SET status = 'failed', claim_id = NULL, session_id = ?,
            next_retry_at = ?, last_error = ?, updated_at = ?
        WHERE project_id = ? AND intent_id = ?
        """,
        (
            session_id,
            _retry_at(body.retry_after_seconds),
            _clean_error(body.error),
            now,
            project_id,
            intent_id,
        ),
    )
    conn.execute(
        "UPDATE intents SET worker = NULL, last_heartbeat_at = NULL "
        "WHERE project_id = ? AND id = ?",
        (project_id, intent_id),
    )
    return _run_row_to_model(_get_run_or_404(conn, project_id, intent_id))
