from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cairn.server.db import get_conn
from cairn.server.services import (
    expire_stale_claims,
    expire_stale_requests,
    get_auth_claim_ttl,
    get_auth_request_or_404,
    get_auth_request_ttl,
    get_auth_control_plane_mode,
    get_project_or_404,
    require_auth_principal,
)

router = APIRouter(tags=["auth-helper"])


def _view(conn, row, *, actor_id: str) -> dict[str, object]:
    if row["helper_actor_id"] is not None and row["helper_actor_id"] != actor_id:
        # Claimed ownership is deliberately non-discoverable to other Helpers.
        from fastapi import HTTPException

        raise HTTPException(404, "Auth request not found")
    target = conn.execute(
        "SELECT login_url FROM auth_target_configs WHERE auth_ref = ?", (row["auth_ref"],)
    ).fetchone()
    payload: dict[str, object] = {
        "id": row["id"],
        "auth_ref": row["auth_ref"],
        # Never echo auth_requests.login_url; only a deployment-provisioned authority
        # may be exposed to the helper.
        "login_url": target["login_url"] if target is not None else None,
        "status": row["status"],
    }
    if row["helper_actor_id"] is not None:
        payload["helper_actor_id"] = row["helper_actor_id"]
    return payload


@router.get(
    "/projects/{project_id}/auth-requests/helper-pending",
)
def list_helper_pending(project_id: str, request: Request):
    with get_conn() as conn:
        principal = require_auth_principal(request, conn, scope="helper.request.read", project_id=project_id)
        get_project_or_404(conn, project_id)
        if get_auth_control_plane_mode(conn) == "legacy":
            expire_stale_claims(conn, get_auth_claim_ttl(conn))
            expire_stale_requests(conn, get_auth_request_ttl(conn))
        rows = conn.execute(
            """
            SELECT id, auth_ref, login_url, status, helper_actor_id
            FROM auth_requests
            WHERE project_id = ? AND status = 'pending'
            ORDER BY created_at, id
            """,
            (project_id,),
        ).fetchall()
        return JSONResponse([_view(conn, row, actor_id=principal.actor_id) for row in rows])


@router.get(
    "/projects/{project_id}/auth-requests/{request_id}/helper-view",
)
def get_helper_view(project_id: str, request_id: str, request: Request):
    with get_conn() as conn:
        principal = require_auth_principal(request, conn, scope="helper.request.read", project_id=project_id)
        get_project_or_404(conn, project_id)
        if get_auth_control_plane_mode(conn) == "legacy":
            expire_stale_claims(conn, get_auth_claim_ttl(conn))
            expire_stale_requests(conn, get_auth_request_ttl(conn))
        row = get_auth_request_or_404(conn, request_id)
        if row["project_id"] != project_id:
            # Avoid cross-project existence leaks from helper-scoped views.
            from fastapi import HTTPException

            raise HTTPException(404, "Auth request not found")
        return JSONResponse(_view(conn, row, actor_id=principal.actor_id))
