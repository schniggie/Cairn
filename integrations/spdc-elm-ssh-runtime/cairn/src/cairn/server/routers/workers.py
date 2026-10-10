from __future__ import annotations

from cairn.server.db import get_conn
from cairn.server.models import WorkerInventoryItem, WorkerInventoryUpsertRequest, WorkerRuntimeHealth, WorkerRuntimeHealthUpsertRequest
from cairn.server.services import (
    dumps_json,
    effective_worker_runtime_health,
    get_project_or_404,
    list_worker_runtime_health,
    loads_json_list,
    loads_json_object,
    upsert_worker_runtime_health,
    utcnow,
)

from fastapi import APIRouter

router = APIRouter(tags=["workers"])


def _worker_item(conn, row, runtime_health: list[WorkerRuntimeHealth] | None = None) -> WorkerInventoryItem:
    health = [effective_worker_runtime_health(conn, item, row) for item in (runtime_health or [])]
    return WorkerInventoryItem(
        name=row["name"],
        type=row["type"],
        model_profile=row["model_profile"],
        model=row["model"] if "model" in row.keys() else None,
        model_context_window=row["model_context_window"] if "model_context_window" in row.keys() else None,
        endpoint=row["endpoint"],
        task_types=loads_json_list(row["task_types_json"]) or [],
        max_running=row["max_running"],
        priority=row["priority"],
        allowed_environments=loads_json_list(row["allowed_environments_json"]),
        question_capability=loads_json_object(row["question_capability_json"]) if "question_capability_json" in row.keys() else None,
        runtime_health=health,
        capability_updated_at=row["capability_updated_at"] if "capability_updated_at" in row.keys() else None,
        capability_source=row["capability_source"] if "capability_source" in row.keys() else None,
        updated_at=row["updated_at"],
    )


@router.get("/workers", response_model=list[WorkerInventoryItem])
def list_workers():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM worker_inventory ORDER BY priority, name").fetchall()
        health_by_worker = _runtime_health_by_worker(conn)
        return [_worker_item(conn, row, health_by_worker.get(row["name"], [])) for row in rows]


@router.get("/projects/{project_id}/workers/capabilities", response_model=list[WorkerInventoryItem])
def list_project_worker_capabilities(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute("SELECT * FROM worker_inventory ORDER BY priority, name").fetchall()
        health_by_worker = _runtime_health_by_worker(conn)
        return [_worker_item(conn, row, health_by_worker.get(row["name"], [])) for row in rows]


@router.put("/dispatcher/workers/health", response_model=list[WorkerRuntimeHealth])
def upsert_worker_health(body: WorkerRuntimeHealthUpsertRequest):
    with get_conn() as conn:
        return [upsert_worker_runtime_health(conn, item) for item in body.health]


@router.put("/workers", response_model=list[WorkerInventoryItem])
def upsert_workers(body: WorkerInventoryUpsertRequest):
    with get_conn() as conn:
        now = utcnow()
        names = [worker.name for worker in body.workers]
        conn.execute("DELETE FROM worker_inventory")
        for worker in body.workers:
            conn.execute(
                """
                INSERT INTO worker_inventory (
                    name, type, model_profile, model, model_context_window, endpoint, task_types_json, max_running,
                    priority, allowed_environments_json, question_capability_json, capability_updated_at,
                    capability_source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker.name,
                    worker.type,
                    worker.model_profile,
                    worker.model,
                    worker.model_context_window,
                    worker.endpoint,
                    dumps_json(worker.task_types) or "[]",
                    worker.max_running,
                    worker.priority,
                    dumps_json(worker.allowed_environments),
                    dumps_json(worker.question_capability),
                    worker.capability_updated_at or now,
                    worker.capability_source or "config",
                    now,
                ),
            )
        rows = conn.execute(
            "SELECT * FROM worker_inventory WHERE name IN (%s) ORDER BY priority, name"
            % ",".join("?" for _ in names),
            tuple(names),
        ).fetchall() if names else []
        return [_worker_item(conn, row) for row in rows]


def _runtime_health_by_worker(conn) -> dict[str, list[WorkerRuntimeHealth]]:
    health_by_worker: dict[str, list[WorkerRuntimeHealth]] = {}
    for health in list_worker_runtime_health(conn):
        health_by_worker.setdefault(health.worker_name, []).append(health)
    return health_by_worker
