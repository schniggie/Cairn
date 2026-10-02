import json

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
    MarkIntentFailedRequest,
)
from cairn.server.services import (
    assemble_poc_brief,
    check_project_active,
    fact_from_row,
    gate_confidence,
    expire_workers,
    get_claimable_open_intent_or_404,
    get_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        now = utcnow()
        if body.worker is not None:
            conn.execute(
                "UPDATE projects SET started_at = COALESCE(started_at, ?) WHERE id = ?",
                (now, project_id),
            )
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        task_kind = body.task_kind
        if task_kind is None and body.description.upper().startswith("VERIFY"):
            task_kind = "verify"
        poc_brief_json = None
        fire_status = None
        if task_kind == "verify":
            brief = assemble_poc_brief(conn, project_id, body.from_, body.description)
            poc_brief_json = brief.model_dump_json()
            fire_status = "pending"
        conn.execute(
            """INSERT INTO intents
               (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at, task_kind, poc_brief, fire_status)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
                task_kind,
                poc_brief_json,
                fire_status,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        row = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (iid, project_id),
        ).fetchone()
        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        conn.execute(
            "UPDATE projects SET started_at = COALESCE(started_at, ?) WHERE id = ?",
            (now, project_id),
        )
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        created: list[Fact] = []
        if body.observations:
            for obs in body.observations:
                gate_confidence(obs.type, obs.confidence)
                fid = next_fact_id(conn, project_id)
                conn.execute(
                    """INSERT INTO facts
                       (id, project_id, description, type, confidence, locations, evidence, verifies, intent_id, oracle_draft, payload_draft)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        fid,
                        project_id,
                        obs.description,
                        obs.type,
                        obs.confidence,
                        json.dumps(obs.locations) if obs.locations else None,
                        obs.evidence,
                        obs.verifies,
                        intent_id,
                        obs.oracle_draft,
                        obs.payload_draft,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM facts WHERE id = ? AND project_id = ?",
                    (fid, project_id),
                ).fetchone()
                created.append(fact_from_row(row, conn, project_id))
            main = created[-1]
        else:
            fid = next_fact_id(conn, project_id)
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
                (fid, project_id, body.description),
            )
            main = Fact(id=fid, description=body.description or "")
            created = [main]
        conn.execute(
            """UPDATE intents
               SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ?,
                   concluded_as = 'success', retry_count = 0,
                   fire_status = CASE WHEN task_kind = 'verify' THEN 'fired' ELSE fire_status END
               WHERE id = ? AND project_id = ?""",
            (main.id, body.worker, now, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=main,
            facts=created,
            intent=intent_to_model(conn, updated, project_id),
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/fail",
    response_model=Intent,
)
def record_intent_failure(project_id: str, intent_id: str, body: MarkIntentFailedRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_workers(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Intent already concluded")

        conn.execute(
            "UPDATE intents SET retry_count = retry_count + 1 WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        )
        row = conn.execute(
            "SELECT retry_count, concluded_as FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        new_concluded = None
        if row["retry_count"] >= body.dead_retry_threshold:
            new_concluded = "dead"
        elif row["retry_count"] >= body.stale_retry_threshold:
            new_concluded = "stale"
        if new_concluded and row["concluded_as"] != "dead":
            conn.execute(
                "UPDATE intents SET concluded_as = ? WHERE id = ? AND project_id = ?",
                (new_concluded, intent_id, project_id),
            )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)
