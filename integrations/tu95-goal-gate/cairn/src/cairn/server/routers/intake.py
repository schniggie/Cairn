from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.intake import (
    claim_intake,
    confirm_intake,
    decide_intake,
    force_activate,
    heartbeat_intake,
    release_intake,
    retry_intake,
    submit_answers,
)
from cairn.server.models import (
    ForceActivateRequest,
    IntakeClaimRequest,
    IntakeClaimResponse,
    IntakeConfirmRequest,
    IntakeDecisionRequest,
    IntakeLeaseRequest,
    IntakeReleaseRequest,
    ProjectDetail,
    SubmitIntakeAnswersRequest,
)


router = APIRouter(tags=["project-intake"])


@router.post(
    "/projects/{project_id}/intake/answers", response_model=ProjectDetail
)
def answer_project_intake(project_id: str, body: SubmitIntakeAnswersRequest):
    with get_conn() as conn:
        return submit_answers(conn, project_id, body)


@router.post(
    "/projects/{project_id}/intake/claim", response_model=IntakeClaimResponse
)
def claim_project_intake(project_id: str, body: IntakeClaimRequest):
    with get_conn() as conn:
        return claim_intake(conn, project_id, body)


@router.post(
    "/projects/{project_id}/intake/heartbeat", response_model=IntakeClaimResponse
)
def heartbeat_project_intake(project_id: str, body: IntakeLeaseRequest):
    with get_conn() as conn:
        return heartbeat_intake(conn, project_id, body)


@router.post(
    "/projects/{project_id}/intake/decision", response_model=ProjectDetail
)
def decide_project_intake(project_id: str, body: IntakeDecisionRequest):
    with get_conn() as conn:
        return decide_intake(conn, project_id, body)


@router.post(
    "/projects/{project_id}/intake/release", response_model=ProjectDetail
)
def release_project_intake(project_id: str, body: IntakeReleaseRequest):
    with get_conn() as conn:
        return release_intake(conn, project_id, body)


@router.post(
    "/projects/{project_id}/intake/retry", response_model=ProjectDetail
)
def retry_project_intake(project_id: str):
    with get_conn() as conn:
        return retry_intake(conn, project_id)


@router.post(
    "/projects/{project_id}/intake/confirm", response_model=ProjectDetail
)
def confirm_project_intake(project_id: str, body: IntakeConfirmRequest):
    with get_conn() as conn:
        return confirm_intake(conn, project_id, body)


@router.post(
    "/projects/{project_id}/intake/force", response_model=ProjectDetail
)
def force_project_intake(project_id: str, body: ForceActivateRequest):
    with get_conn() as conn:
        return force_activate(conn, project_id, body)
