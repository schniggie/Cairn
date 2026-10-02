from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterable

from fastapi import HTTPException
from pydantic import TypeAdapter

from cairn.server.db import CommittedHTTPException
from cairn.server.models import (
    CreateHintInline,
    CreateProjectRequest,
    ForceActivateRequest,
    GoalSpec,
    IntakeAnswerTranscriptEntry,
    IntakeClaimRequest,
    IntakeClaimResponse,
    IntakeConfirmRequest,
    IntakeContextTranscriptEntry,
    IntakeDecisionRequest,
    IntakeEditTranscriptEntry,
    IntakeLeaseRequest,
    IntakeProposal,
    IntakeQuestion,
    IntakeReady,
    IntakeReleaseRequest,
    IntakeTranscriptEntry,
    ProjectDetail,
    SubmitIntakeAnswersRequest,
)
from cairn.server.services import (
    build_project_detail,
    get_intake_auto_activate,
    get_intake_lease_timeout,
    get_intake_or_404,
    get_max_intake_attempts,
    get_max_intake_clarification_rounds,
    get_project_or_404,
    next_hint_id,
    next_project_id,
    utcnow,
)


_QUESTION_KEY_RE = re.compile(
    r"^(resources|success|constraints|authorization|access)\.[a-z0-9_]+$"
)
_RAW_HINTS_ADAPTER = TypeAdapter(list[CreateHintInline])
_TRANSCRIPT_ADAPTER = TypeAdapter(list[IntakeTranscriptEntry])
_QUESTIONS_ADAPTER = TypeAdapter(list[IntakeQuestion])


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_raw_hints(row: sqlite3.Row) -> list[CreateHintInline]:
    return _RAW_HINTS_ADAPTER.validate_python(json.loads(row["raw_hints_json"]))


def _load_transcript(row: sqlite3.Row) -> list[IntakeTranscriptEntry]:
    return _TRANSCRIPT_ADAPTER.validate_python(json.loads(row["transcript_json"]))


def _load_pending_questions(row: sqlite3.Row) -> list[IntakeQuestion]:
    return _QUESTIONS_ADAPTER.validate_python(json.loads(row["pending_questions_json"]))


def _clean_error(error: str) -> str:
    # last_error 面向用户展示，只保留单行摘要，避免把内部堆栈写入协议状态。
    return " ".join(error.split())[:500]


def create_pending_project(
    conn: sqlite3.Connection, body: CreateProjectRequest
) -> ProjectDetail:
    project_id = next_project_id(conn)
    now = utcnow()
    raw_hints = [hint.model_dump(mode="json") for hint in body.hints or []]
    conn.execute(
        "INSERT INTO projects (id, title, status, bootstrap_enabled, max_workers, created_at) "
        "VALUES (?, ?, 'preparing', ?, ?, ?)",
        (
            project_id,
            body.title,
            body.bootstrap_enabled,
            body.max_workers if body.max_workers is not None else 8,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO project_intakes (
            project_id, raw_origin, raw_goal, raw_hints_json,
            transcript_json, pending_questions_json, revision,
            clarification_rounds, auto_activate, attempt_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, '[]', '[]', 1, 0, ?, 0, ?, ?)
        """,
        (
            project_id,
            body.origin,
            body.goal,
            _json_dump(raw_hints),
            None if body.auto_activate is None else int(body.auto_activate),
            now,
            now,
        ),
    )
    return build_project_detail(conn, project_id)


def _claim_response(row: sqlite3.Row) -> IntakeClaimResponse:
    assert row["review_worker"] is not None
    assert row["review_claim_id"] is not None
    return IntakeClaimResponse(
        project_id=row["project_id"],
        worker=row["review_worker"],
        claim_id=row["review_claim_id"],
        revision=row["revision"],
        raw_origin=row["raw_origin"],
        raw_goal=row["raw_goal"],
        raw_hints=_load_raw_hints(row),
        transcript=_load_transcript(row),
    )


def _validate_revision(row: sqlite3.Row, revision: int) -> None:
    if row["revision"] != revision:
        raise HTTPException(409, "Project intake revision has changed")


def _validate_lease(row: sqlite3.Row, body: IntakeLeaseRequest) -> None:
    _validate_revision(row, body.revision)
    if row["review_worker"] is None or row["review_claim_id"] is None:
        raise HTTPException(409, "Project intake is not currently claimed")
    if row["review_worker"] != body.worker or row["review_claim_id"] != body.claim_id:
        raise HTTPException(409, "Project intake claim is held by another execution")


def _record_failure(
    conn: sqlite3.Connection,
    project_id: str,
    error: str,
    *,
    now: str | None = None,
) -> None:
    now = now or utcnow()
    intake = get_intake_or_404(conn, project_id)
    attempts = intake["attempt_count"] + 1
    status = "intake_failed" if attempts >= get_max_intake_attempts(conn) else "preparing"
    conn.execute(
        """
        UPDATE project_intakes
        SET attempt_count = ?, last_error = ?, pending_questions_json = '[]',
            waiting_since = NULL, review_worker = NULL, review_claim_id = NULL,
            review_started_at = NULL, review_last_heartbeat_at = NULL,
            updated_at = ?
        WHERE project_id = ?
        """,
        (attempts, _clean_error(error), now, project_id),
    )
    conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))


def expire_intake_leases(
    conn: sqlite3.Connection, project_id: str | None = None
) -> set[str]:
    timeout = get_intake_lease_timeout(conn)
    now = utcnow()
    query = """
        SELECT pi.project_id
        FROM project_intakes pi
        JOIN projects p ON p.id = pi.project_id
        WHERE p.status = 'preparing'
          AND pi.review_worker IS NOT NULL
          AND pi.review_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(pi.review_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple[object, ...] = (now, timeout)
    if project_id is not None:
        query += " AND pi.project_id = ?"
        params = (now, timeout, project_id)
    rows = conn.execute(query, params).fetchall()
    for row in rows:
        _record_failure(conn, row["project_id"], "Intake worker lease expired", now=now)
    return {row["project_id"] for row in rows}


def claim_intake(
    conn: sqlite3.Connection, project_id: str, body: IntakeClaimRequest
) -> IntakeClaimResponse:
    # claim_id 由可信 Dispatcher 为每次新认领生成全新 UUID；服务端只保存当前租约。
    expired = expire_intake_leases(conn, project_id)
    project = get_project_or_404(conn, project_id)
    if project["status"] != "preparing":
        if project_id in expired:
            raise CommittedHTTPException(409, "Project intake lease expired")
        raise HTTPException(409, f"Project intake is {project['status']}")
    row = get_intake_or_404(conn, project_id)
    _validate_revision(row, body.revision)

    if row["review_worker"] is not None:
        if row["review_worker"] != body.worker or row["review_claim_id"] != body.claim_id:
            raise HTTPException(409, "Project intake is currently claimed")
        now = utcnow()
        conn.execute(
            "UPDATE project_intakes SET review_last_heartbeat_at = ?, updated_at = ? "
            "WHERE project_id = ?",
            (now, now, project_id),
        )
        return _claim_response(get_intake_or_404(conn, project_id))

    now = utcnow()
    conn.execute(
        """
        UPDATE project_intakes
        SET review_worker = ?, review_claim_id = ?, review_started_at = ?,
            review_last_heartbeat_at = ?, updated_at = ?
        WHERE project_id = ?
        """,
        (body.worker, body.claim_id, now, now, now, project_id),
    )
    return _claim_response(get_intake_or_404(conn, project_id))


def heartbeat_intake(
    conn: sqlite3.Connection, project_id: str, body: IntakeLeaseRequest
) -> IntakeClaimResponse:
    expired = expire_intake_leases(conn, project_id)
    if project_id in expired:
        raise CommittedHTTPException(409, "Project intake lease expired")
    project = get_project_or_404(conn, project_id)
    if project["status"] != "preparing":
        raise HTTPException(409, f"Project intake is {project['status']}")
    row = get_intake_or_404(conn, project_id)
    _validate_lease(row, body)
    now = utcnow()
    conn.execute(
        "UPDATE project_intakes SET review_last_heartbeat_at = ?, updated_at = ? "
        "WHERE project_id = ?",
        (now, now, project_id),
    )
    return _claim_response(get_intake_or_404(conn, project_id))


def release_intake(
    conn: sqlite3.Connection, project_id: str, body: IntakeReleaseRequest
) -> ProjectDetail:
    expired = expire_intake_leases(conn, project_id)
    if project_id in expired:
        raise CommittedHTTPException(409, "Project intake lease expired")
    project = get_project_or_404(conn, project_id)
    if project["status"] != "preparing":
        raise HTTPException(409, f"Project intake is {project['status']}")
    row = get_intake_or_404(conn, project_id)
    _validate_lease(row, body)
    _record_failure(conn, project_id, body.error)
    return build_project_detail(conn, project_id)


def _historical_question_keys(
    transcript: Iterable[IntakeTranscriptEntry], pending: Iterable[IntakeQuestion]
) -> set[str]:
    keys = {question.key for question in pending}
    for entry in transcript:
        if isinstance(entry, IntakeAnswerTranscriptEntry):
            keys.add(entry.key)
    return keys


def _validate_question_semantics(row: sqlite3.Row, body: IntakeDecisionRequest) -> str | None:
    assert body.questions is not None
    keys = [question.key for question in body.questions]
    if any(_QUESTION_KEY_RE.fullmatch(key) is None for key in keys):
        return "Goal Gate returned an invalid question key"
    if len(set(keys)) != len(keys):
        return "Goal Gate returned duplicate question keys"
    historical = _historical_question_keys(
        _load_transcript(row), _load_pending_questions(row)
    )
    if historical.intersection(keys):
        return "Goal Gate repeated an already asked question key"
    return None


def _activate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    origin: str,
    goal: str,
    spec: GoalSpec | None,
    mode: str,
) -> ProjectDetail:
    intake = get_intake_or_404(conn, project_id)
    existing = conn.execute(
        "SELECT id FROM facts WHERE project_id = ? AND id IN ('origin', 'goal')",
        (project_id,),
    ).fetchall()
    if existing:
        raise HTTPException(409, "Project graph is already initialized")

    now = utcnow()
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES ('origin', ?, ?)",
        (project_id, origin),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES ('goal', ?, ?)",
        (project_id, goal),
    )
    for hint in _load_raw_hints(intake):
        hint_id = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (hint_id, project_id, hint.content, hint.creator, now),
        )
    spec_json = spec.model_dump_json() if spec is not None else None
    conn.execute(
        """
        UPDATE project_intakes
        SET normalized_origin = ?, normalized_goal = ?, goal_spec_json = ?,
            activation_mode = ?, pending_questions_json = '[]', waiting_since = NULL,
            pending_proposal_json = NULL,
            review_worker = NULL, review_claim_id = NULL, review_started_at = NULL,
            review_last_heartbeat_at = NULL, last_error = NULL, updated_at = ?
        WHERE project_id = ?
        """,
        (origin, goal, spec_json, mode, now, project_id),
    )
    conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (project_id,))
    return build_project_detail(conn, project_id)


def _auto_activate_enabled(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    # 项目级 auto_activate 优先；NULL 时跟随 Server 设置 intake_auto_activate。
    if row["auto_activate"] is not None:
        return bool(row["auto_activate"])
    return get_intake_auto_activate(conn)


def _hold_for_review(
    conn: sqlite3.Connection, project_id: str, ready: IntakeReady
) -> ProjectDetail:
    # 未开启 auto_activate：只保存待确认 proposal，不写知识图，等用户确认。
    proposal = IntakeProposal(
        origin=ready.origin,
        goal=ready.goal,
        spec=ready.spec,
        notices=ready.notices,
    )
    now = utcnow()
    conn.execute(
        """
        UPDATE project_intakes
        SET pending_proposal_json = ?, pending_questions_json = '[]',
            waiting_since = NULL, review_worker = NULL, review_claim_id = NULL,
            review_started_at = NULL, review_last_heartbeat_at = NULL,
            last_error = NULL, updated_at = ?
        WHERE project_id = ?
        """,
        (proposal.model_dump_json(), now, project_id),
    )
    conn.execute(
        "UPDATE projects SET status = 'ready_review' WHERE id = ?", (project_id,)
    )
    return build_project_detail(conn, project_id)


def decide_intake(
    conn: sqlite3.Connection, project_id: str, body: IntakeDecisionRequest
) -> ProjectDetail:
    expired = expire_intake_leases(conn, project_id)
    if project_id in expired:
        raise CommittedHTTPException(409, "Project intake lease expired")
    project = get_project_or_404(conn, project_id)
    row = get_intake_or_404(conn, project_id)
    if project["status"] == "active" and row["activation_mode"] == "gate" and body.ready is not None:
        return build_project_detail(conn, project_id)
    if project["status"] == "ready_review" and body.ready is not None:
        # ready 已转入待确认：同一 decision 的重试幂等返回，不重复写 proposal。
        return build_project_detail(conn, project_id)
    if project["status"] != "preparing":
        raise HTTPException(409, f"Project intake is {project['status']}")
    _validate_lease(row, body)

    if body.ready is not None:
        if _auto_activate_enabled(conn, row):
            return _activate(
                conn,
                project_id,
                origin=body.ready.origin,
                goal=body.ready.goal,
                spec=body.ready.spec,
                mode="gate",
            )
        return _hold_for_review(conn, project_id, body.ready)

    semantic_error = _validate_question_semantics(row, body)
    if semantic_error is not None:
        _record_failure(conn, project_id, semantic_error)
        return build_project_detail(conn, project_id)

    assert body.questions is not None
    now = utcnow()
    max_rounds = get_max_intake_clarification_rounds(conn)
    if row["clarification_rounds"] >= max_rounds:
        conn.execute(
            """
            UPDATE project_intakes
            SET pending_questions_json = '[]', waiting_since = NULL,
                review_worker = NULL, review_claim_id = NULL,
                review_started_at = NULL, review_last_heartbeat_at = NULL,
                last_error = ?, updated_at = ?
            WHERE project_id = ?
            """,
            ("Maximum intake clarification rounds reached", now, project_id),
        )
        conn.execute(
            "UPDATE projects SET status = 'intake_failed' WHERE id = ?", (project_id,)
        )
        return build_project_detail(conn, project_id)

    questions = [
        IntakeQuestion(
            id=f"q{row['revision']:03d}_{index:02d}",
            key=question.key,
            question=question.question,
            why_blocking=question.why_blocking,
        )
        for index, question in enumerate(body.questions, start=1)
    ]
    payload = [question.model_dump(mode="json") for question in questions]
    conn.execute(
        """
        UPDATE project_intakes
        SET pending_questions_json = ?, clarification_rounds = clarification_rounds + 1,
            waiting_since = ?, review_worker = NULL, review_claim_id = NULL,
            review_started_at = NULL, review_last_heartbeat_at = NULL,
            last_error = NULL, updated_at = ?
        WHERE project_id = ?
        """,
        (_json_dump(payload), now, now, project_id),
    )
    conn.execute(
        "UPDATE projects SET status = 'waiting_input' WHERE id = ?", (project_id,)
    )
    return build_project_detail(conn, project_id)


def confirm_intake(
    conn: sqlite3.Connection, project_id: str, body: IntakeConfirmRequest
) -> ProjectDetail:
    project = get_project_or_404(conn, project_id)
    row = get_intake_or_404(conn, project_id)
    if project["status"] == "active" and row["activation_mode"] == "gate":
        # 重复 confirm 幂等返回当前项目，不重复插入 Origin/Goal/hints。
        return build_project_detail(conn, project_id)
    if project["status"] != "ready_review":
        raise HTTPException(409, f"Project intake is {project['status']}")
    _validate_revision(row, body.revision)
    if row["pending_proposal_json"] is None:
        raise HTTPException(409, "Project intake has no pending proposal")
    proposal = IntakeProposal.model_validate_json(row["pending_proposal_json"])
    return _activate(
        conn,
        project_id,
        origin=proposal.origin,
        goal=proposal.goal,
        spec=proposal.spec,
        mode="gate",
    )


def submit_answers(
    conn: sqlite3.Connection,
    project_id: str,
    body: SubmitIntakeAnswersRequest,
) -> ProjectDetail:
    project = get_project_or_404(conn, project_id)
    row = get_intake_or_404(conn, project_id)
    _validate_revision(row, body.revision)
    transcript = _load_transcript(row)

    if project["status"] == "waiting_input":
        pending = _load_pending_questions(row)
        pending_by_id = {question.id: question for question in pending}
        answer_ids = [answer.question_id for answer in body.answers]
        if len(answer_ids) != len(set(answer_ids)) or set(answer_ids) != set(pending_by_id):
            raise HTTPException(422, "All pending questions must be answered exactly once")
        for answer in body.answers:
            question = pending_by_id[answer.question_id]
            transcript.append(
                IntakeAnswerTranscriptEntry(
                    question_id=question.id,
                    key=question.key,
                    question=question.question,
                    answer=answer.answer,
                    unknown=answer.unknown,
                )
            )
    elif project["status"] == "ready_review":
        if body.answers:
            raise HTTPException(
                422,
                "Ready review accepts only empty answers with edited origin/goal",
            )
        if body.edited_origin is None or body.edited_goal is None:
            raise HTTPException(422, "Edited origin and goal must not be empty")
        # 编辑事件不保存文本全文，只记长度与短预览；编辑后的文本本身覆盖 raw_*。
        transcript.append(
            IntakeEditTranscriptEntry(
                origin_length=len(body.edited_origin),
                goal_length=len(body.edited_goal),
                origin_preview=body.edited_origin[:120],
                goal_preview=body.edited_goal[:120],
            )
        )
    elif project["status"] == "intake_failed":
        if body.answers or body.additional_context is None:
            raise HTTPException(
                422,
                "Failed intake accepts only empty answers with non-empty additional_context",
            )
    else:
        raise HTTPException(409, f"Project intake is {project['status']}")

    if body.additional_context is not None:
        transcript.append(IntakeContextTranscriptEntry(content=body.additional_context))

    raw_origin = row["raw_origin"]
    raw_goal = row["raw_goal"]
    if project["status"] == "ready_review":
        raw_origin = body.edited_origin
        raw_goal = body.edited_goal

    now = utcnow()
    transcript_payload = [entry.model_dump(mode="json") for entry in transcript]
    conn.execute(
        """
        UPDATE project_intakes
        SET transcript_json = ?, pending_questions_json = '[]', revision = revision + 1,
            attempt_count = 0, waiting_since = NULL, last_error = NULL,
            review_worker = NULL, review_claim_id = NULL,
            review_started_at = NULL, review_last_heartbeat_at = NULL,
            pending_proposal_json = NULL, raw_origin = ?, raw_goal = ?, updated_at = ?
        WHERE project_id = ?
        """,
        (_json_dump(transcript_payload), raw_origin, raw_goal, now, project_id),
    )
    conn.execute("UPDATE projects SET status = 'preparing' WHERE id = ?", (project_id,))
    return build_project_detail(conn, project_id)


def retry_intake(conn: sqlite3.Connection, project_id: str) -> ProjectDetail:
    project = get_project_or_404(conn, project_id)
    if project["status"] != "intake_failed":
        raise HTTPException(409, f"Project intake is {project['status']}")
    get_intake_or_404(conn, project_id)
    now = utcnow()
    conn.execute(
        """
        UPDATE project_intakes
        SET revision = revision + 1, attempt_count = 0, last_error = NULL,
            pending_questions_json = '[]',
            waiting_since = NULL, review_worker = NULL, review_claim_id = NULL,
            review_started_at = NULL, review_last_heartbeat_at = NULL, updated_at = ?
        WHERE project_id = ?
        """,
        (now, project_id),
    )
    conn.execute("UPDATE projects SET status = 'preparing' WHERE id = ?", (project_id,))
    return build_project_detail(conn, project_id)


def _forced_origin(row: sqlite3.Row) -> str:
    transcript = _load_transcript(row)
    additions: list[str] = []
    for entry in transcript:
        if isinstance(entry, IntakeAnswerTranscriptEntry):
            value = "unknown" if entry.unknown else entry.answer
            additions.append(f"{entry.question}: {value}")
        elif isinstance(entry, IntakeContextTranscriptEntry):
            additions.append(entry.content)
        # 编辑事件的文本已并入 raw_origin/raw_goal，不再重复拼接。
    if not additions:
        return row["raw_origin"]
    return row["raw_origin"] + "\n\nAdditional intake context:\n" + "\n".join(additions)


def force_activate(
    conn: sqlite3.Connection,
    project_id: str,
    body: ForceActivateRequest,
) -> ProjectDetail:
    del body  # Literal[True] 已在 HTTP 模型层完成显式确认校验。
    project = get_project_or_404(conn, project_id)
    row = get_intake_or_404(conn, project_id)
    if project["status"] == "active" and row["activation_mode"] == "forced":
        return build_project_detail(conn, project_id)
    if project["status"] not in {"waiting_input", "ready_review", "intake_failed"}:
        raise HTTPException(409, f"Project intake is {project['status']}")
    # force 放弃 proposal 按原始输入激活；_activate 同事务清空 pending_proposal_json。
    return _activate(
        conn,
        project_id,
        origin=_forced_origin(row),
        goal=row["raw_goal"],
        spec=None,
        mode="forced",
    )
