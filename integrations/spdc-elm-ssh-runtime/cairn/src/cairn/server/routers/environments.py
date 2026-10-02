from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import ProviderEndpointPublic, ProviderEndpointSecret, ProviderEndpointUpsert, WorkEnvironmentPublic, WorkEnvironmentUpsert
from cairn.server.services import (
    create_environment_healthcheck_requests,
    effective_worker_runtime_health,
    environment_row_to_public,
    get_environment_provider_endpoint_or_404,
    get_environment_or_404,
    list_worker_runtime_health,
    slugify_environment_id,
    upsert_environment_provider_endpoint,
    utcnow,
    validate_environment_body,
)

router = APIRouter(tags=["environments"])


@router.get("/environments", response_model=list[WorkEnvironmentPublic])
def list_environments(include_secrets: bool = False):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM work_environments ORDER BY backend, created_at, id").fetchall()
        return [environment_row_to_public(row, conn=conn, include_secret=False) for row in rows]


@router.post("/environments", response_model=WorkEnvironmentPublic, status_code=201)
def create_environment(body: WorkEnvironmentUpsert):
    validate_environment_body(body)
    with get_conn() as conn:
        environment_id = _choose_environment_id(conn, body.id or slugify_environment_id(body.label))
        now = utcnow()
        conn.execute(
            """
            INSERT INTO work_environments (
                id, label, backend, ssh_command, workspace_root, cleanup_json, terminal_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                environment_id,
                body.label,
                body.backend,
                body.ssh_command,
                body.workspace_root,
                json.dumps(body.cleanup or {"completed_action": "stop"}, ensure_ascii=True, sort_keys=True),
                json.dumps(body.terminal or {"mode": "none"}, ensure_ascii=True, sort_keys=True),
                now,
                now,
            ),
        )
        for endpoint in body.provider_endpoints:
            upsert_environment_provider_endpoint(conn, environment_id, endpoint)
        return get_environment_or_404(conn, environment_id)


@router.put("/environments/{environment_id}", response_model=WorkEnvironmentPublic)
def update_environment(environment_id: str, body: WorkEnvironmentUpsert):
    if environment_id == "docker-default" and body.backend != "docker":
        raise HTTPException(400, "docker-default cannot be changed to SSH")
    validate_environment_body(body)
    with get_conn() as conn:
        now = utcnow()
        conn.execute(
            """
            UPDATE work_environments
            SET label = ?,
                backend = ?,
                ssh_command = ?,
                workspace_root = ?,
                cleanup_json = ?,
                terminal_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                body.label,
                body.backend,
                body.ssh_command,
                body.workspace_root,
                json.dumps(body.cleanup or {"completed_action": "stop"}, ensure_ascii=True, sort_keys=True),
                json.dumps(body.terminal or {"mode": "none"}, ensure_ascii=True, sort_keys=True),
                now,
                environment_id,
            ),
        )
        if conn.total_changes == 0:
            get_environment_or_404(conn, environment_id)
        for endpoint in body.provider_endpoints:
            upsert_environment_provider_endpoint(conn, environment_id, endpoint)
        return get_environment_or_404(conn, environment_id)


@router.delete("/environments/{environment_id}", status_code=204)
def delete_environment(environment_id: str):
    if environment_id == "docker-default":
        raise HTTPException(400, "docker-default cannot be deleted")
    with get_conn() as conn:
        get_environment_or_404(conn, environment_id)
        conn.execute("DELETE FROM work_environments WHERE id = ?", (environment_id,))


@router.get("/environments/{environment_id}/endpoints", response_model=list[ProviderEndpointPublic])
def list_environment_endpoints(environment_id: str):
    with get_conn() as conn:
        environment = get_environment_or_404(conn, environment_id)
        return environment.provider_endpoints


@router.post("/environments/{environment_id}/endpoints", response_model=ProviderEndpointPublic, status_code=201)
def create_environment_endpoint(environment_id: str, body: ProviderEndpointUpsert):
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT 1
            FROM environment_provider_endpoints
            WHERE environment_id = ? AND endpoint_id = ?
            """,
            (environment_id, body.id),
        ).fetchone()
        if existing is not None:
            raise HTTPException(409, "Provider endpoint already exists")
        return upsert_environment_provider_endpoint(conn, environment_id, body)


@router.put("/environments/{environment_id}/endpoints/{endpoint_id}", response_model=ProviderEndpointPublic)
def update_environment_endpoint(environment_id: str, endpoint_id: str, body: ProviderEndpointUpsert):
    if body.id != endpoint_id:
        raise HTTPException(400, "Endpoint id cannot be changed")
    with get_conn() as conn:
        get_environment_provider_endpoint_or_404(conn, environment_id, endpoint_id)
        return upsert_environment_provider_endpoint(conn, environment_id, body)


@router.get("/environments/{environment_id}/endpoints/{endpoint_id}")
def get_environment_endpoint(
    environment_id: str,
    endpoint_id: str,
    include_secret: bool = False,
) -> ProviderEndpointPublic | ProviderEndpointSecret:
    with get_conn() as conn:
        return get_environment_provider_endpoint_or_404(
            conn,
            environment_id,
            endpoint_id,
            include_secret=include_secret,
        )


@router.delete("/environments/{environment_id}/endpoints/{endpoint_id}", status_code=204)
def delete_environment_endpoint(environment_id: str, endpoint_id: str):
    with get_conn() as conn:
        get_environment_provider_endpoint_or_404(conn, environment_id, endpoint_id)
        conn.execute(
            """
            DELETE FROM environment_provider_endpoints
            WHERE environment_id = ? AND endpoint_id = ?
            """,
            (environment_id, endpoint_id),
        )


@router.post("/environments/{environment_id}/healthcheck")
def healthcheck_environment(environment_id: str):
    with get_conn() as conn:
        environment = get_environment_or_404(conn, environment_id)
        if environment.backend != "ssh":
            result = {
                "environment_id": environment.id,
                "backend": environment.backend,
                "status": "skipped",
                "checks": [
                    {
                        "name": "docker",
                        "status": "skipped",
                        "duration_ms": 0,
                        "command": "-",
                        "stdout": "",
                        "stderr": "Docker healthchecks run in the dispatcher startup matrix.",
                    }
                ],
            }
        else:
            result = {
                "environment_id": environment.id,
                "backend": environment.backend,
                "status": "delegated",
                "checks": [
                    {
                        "name": "ssh",
                        "status": "delegated",
                        "duration_ms": 0,
                        "command": "-",
                        "stdout": "",
                        "stderr": "SSH healthchecks run in the dispatcher execution plane.",
                    }
                ],
            }
        _append_endpoint_healthchecks(result, environment)
        _append_runtime_healthchecks(conn, result, environment.id)
        conn.execute(
            "UPDATE work_environments SET last_health_status = ?, last_healthcheck_json = ?, updated_at = ? WHERE id = ?",
            (result.get("status"), json.dumps(result, ensure_ascii=True), utcnow(), environment_id),
        )
        return result


@router.post("/environments/{environment_id}/healthcheck-requests", status_code=201)
def create_environment_healthcheck_request(environment_id: str):
    with get_conn() as conn:
        return create_environment_healthcheck_requests(conn, environment_id)


def _choose_environment_id(conn: sqlite3.Connection, base: str) -> str:
    candidate = base
    index = 2
    while conn.execute("SELECT 1 FROM work_environments WHERE id = ?", (candidate,)).fetchone():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _append_endpoint_healthchecks(result: dict, environment: WorkEnvironmentPublic) -> None:
    checks = result.setdefault("checks", [])
    endpoint_statuses: list[str] = []
    for endpoint in environment.provider_endpoints:
        missing = []
        if not endpoint.base_url:
            missing.append("base_url")
        if endpoint.type == "pi" and not endpoint.provider_api:
            missing.append("provider_api")
        if not endpoint.has_api_key:
            missing.append("api_key")
        status = "ok" if not missing else "failed"
        endpoint_statuses.append(status)
        checks.append(
            {
                "name": f"endpoint:{endpoint.id}",
                "status": status,
                "duration_ms": 0,
                "command": "-",
                "stdout": "",
                "stderr": "" if not missing else f"missing fields: {', '.join(missing)}",
            }
        )
    if endpoint_statuses and any(status != "ok" for status in endpoint_statuses):
        result["status"] = "failed"


def _append_runtime_healthchecks(conn: sqlite3.Connection, result: dict, environment_id: str) -> None:
    checks = result.setdefault("checks", [])
    statuses: list[str] = []
    for health in list_worker_runtime_health(conn):
        if health.environment_id != environment_id:
            continue
        effective = effective_worker_runtime_health(conn, health)
        status = _runtime_health_display_status(effective)
        detail = effective.detail or {}
        statuses.append(status)
        checks.append(
            {
                "name": f"worker:{effective.worker_name}",
                "status": status,
                "duration_ms": detail.get("duration_ms") or 0,
                "command": _health_command(detail),
                "stdout": _health_stdout(detail),
                "stderr": _health_stderr(detail),
            }
        )
    if any(status == "unhealthy" for status in statuses):
        result["status"] = "unhealthy"
    elif statuses and all(status == "ok" for status in statuses) and result.get("status") in {"delegated", "skipped"}:
        result["status"] = "ok"
    elif statuses and result.get("status") in {"delegated", "skipped"}:
        result["status"] = "unknown"


def _runtime_health_display_status(health) -> str:
    if (health.detail or {}).get("reason") == "stale":
        return "stale"
    return health.status


def _health_stdout(detail: dict) -> str:
    parts = []
    if detail.get("http_status"):
        parts.append(f"http={detail['http_status']}")
    if detail.get("returncode") is not None:
        parts.append(f"returncode={detail['returncode']}")
    if detail.get("duration_ms") is not None:
        parts.append(f"duration_ms={detail['duration_ms']}")
    if detail.get("response_preview"):
        parts.append(str(detail["response_preview"]))
    return " ".join(parts)


def _health_command(detail: dict) -> str:
    command = str(detail.get("command") or "")
    if not command:
        return "-"
    compact = " ".join(command.split())
    return compact if len(compact) <= 180 else f"{compact[:177]}..."


def _health_stderr(detail: dict) -> str:
    if detail.get("stderr_preview"):
        return str(detail["stderr_preview"])
    if detail.get("reason"):
        return str(detail["reason"])
    return ""
