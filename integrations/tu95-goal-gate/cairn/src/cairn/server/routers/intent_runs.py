from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.intent_runtime import (
    claim_run,
    conclude_run,
    fail_run,
    heartbeat_run,
    yield_run,
)
from cairn.server.models import (
    ConcludeResponse,
    IntentRun,
    IntentRunClaimRequest,
    IntentRunClaimResponse,
    IntentRunConcludeRequest,
    IntentRunFailRequest,
    IntentRunLeaseRequest,
    IntentRunYieldRequest,
)


router = APIRouter(tags=["intent-runtime"])


@router.post(
    "/projects/{project_id}/intents/{intent_id}/run/claim",
    response_model=IntentRunClaimResponse,
)
def claim_intent_run(
    project_id: str, intent_id: str, body: IntentRunClaimRequest
):
    with get_conn() as conn:
        return claim_run(conn, project_id, intent_id, body)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/run/heartbeat",
    response_model=IntentRun,
)
def heartbeat_intent_run(
    project_id: str, intent_id: str, body: IntentRunLeaseRequest
):
    with get_conn() as conn:
        return heartbeat_run(conn, project_id, intent_id, body)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/run/yield",
    response_model=IntentRun,
)
def yield_intent_run(
    project_id: str, intent_id: str, body: IntentRunYieldRequest
):
    with get_conn() as conn:
        return yield_run(conn, project_id, intent_id, body)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/run/conclude",
    response_model=ConcludeResponse,
)
def conclude_intent_run(
    project_id: str, intent_id: str, body: IntentRunConcludeRequest
):
    with get_conn() as conn:
        return conclude_run(conn, project_id, intent_id, body)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/run/fail",
    response_model=IntentRun,
)
def fail_intent_run(
    project_id: str, intent_id: str, body: IntentRunFailRequest
):
    with get_conn() as conn:
        return fail_run(conn, project_id, intent_id, body)
