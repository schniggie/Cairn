from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def create_active_project(client: TestClient) -> str:
    created = client.post(
        "/projects",
        json={"title": "runtime", "origin": "start", "goal": "finish"},
    ).json()
    project_id = created["project"]["id"]
    claim = client.post(
        f"/projects/{project_id}/intake/claim",
        json={"worker": "reviewer", "claim_id": "intake-1", "revision": 1},
    )
    assert claim.status_code == 200
    activated = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "intake-1",
            "revision": 1,
            "ready": {
                "origin": "start",
                "goal": "finish",
                "spec": {
                    "objective": "finish",
                    "success_criteria": [],
                    "resources": [],
                    "constraints": [],
                    "unknowns": [],
                },
            },
        },
    )
    assert activated.status_code == 200
    assert activated.json()["project"]["status"] == "ready_review"
    confirmed = client.post(
        f"/projects/{project_id}/intake/confirm", json={"revision": 1}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["project"]["status"] == "active"
    intent = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner"},
    )
    assert intent.status_code == 201
    assert intent.json()["id"] == "i001"
    return project_id


def run_url(project_id: str, operation: str) -> str:
    return f"/projects/{project_id}/intents/i001/run/{operation}"


def claim_run(
    client: TestClient,
    project_id: str,
    *,
    claim_id: str,
    worker: str = "explorer",
    worker_type: str = "codex",
):
    return client.post(
        run_url(project_id, "claim"),
        json={"worker": worker, "worker_type": worker_type, "claim_id": claim_id},
    )


def test_run_claim_is_atomic_and_only_claim_id_is_idempotent(client: TestClient) -> None:
    project_id = create_active_project(client)
    first = claim_run(client, project_id, claim_id="run-1")
    assert first.status_code == 200
    assert first.json()["execution_mode"] == "new"
    assert first.json()["attempt_count"] == 1
    assert first.json()["status"] == "running"
    assert first.json()["worker_name"] == "explorer"
    assert first.json()["has_session"] is False

    repeated = claim_run(client, project_id, claim_id="run-1")
    assert repeated.status_code == 200
    assert repeated.json()["attempt_count"] == 1
    assert claim_run(client, project_id, claim_id="run-2").status_code == 409

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] == "explorer"
    assert detail["intent_runs"] == [
        {
            "intent_id": "i001",
            "status": "running",
            "attempt_count": 1,
            "no_progress_count": 0,
            "worker_type": "codex",
            "worker_name": "explorer",
            "has_session": False,
            "next_retry_at": None,
            "updated_at": detail["intent_runs"][0]["updated_at"],
        }
    ]
    assert "session_id" not in detail["intent_runs"][0]
    assert "claim_id" not in detail["intent_runs"][0]

    # 一旦存在任意 Run，旧式接口不再允许混用。
    assert client.post(
        f"/projects/{project_id}/intents/i001/heartbeat", json={"worker": "explorer"}
    ).status_code == 409
    assert client.post(
        f"/projects/{project_id}/intents/i001/release", json={"worker": "explorer"}
    ).status_code == 409
    assert client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "wrong path"},
    ).status_code == 409


def test_yield_preserves_intent_and_merges_cumulative_evidence(client: TestClient) -> None:
    project_id = create_active_project(client)
    assert claim_run(client, project_id, claim_id="run-1").status_code == 200
    artifact_a = {"path": "artifacts/progress.json", "sha256": "a" * 64, "size": 10}
    first = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "session_id": "session-1",
            "checkpoint": {"progress": "first", "next": "continue"},
            "evidence": {
                "artifacts": [artifact_a],
                "cursors": {"tested": 10},
                "milestones": [{"id": "scan-started", "artifact": artifact_a["path"]}],
            },
            "max_no_progress_slices": 3,
            "retry_after_seconds": 15,
        },
    )
    assert first.status_code == 200
    assert first.json()["status"] == "yielded"
    assert first.json()["no_progress_count"] == 0
    assert first.json()["has_session"] is True
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["facts"] == [
        {"id": "origin", "description": "start"},
        {"id": "goal", "description": "finish"},
    ]
    assert detail["intents"][0]["to"] is None
    assert detail["intents"][0]["worker"] is None
    assert detail["intent_runs"][0]["worker_name"] == "explorer"
    assert detail["intent_runs"][0]["has_session"] is True
    assert "session_id" not in detail["intent_runs"][0]
    assert client.post(
        f"/projects/{project_id}/intents/i001/heartbeat", json={"worker": "explorer"}
    ).status_code == 409
    assert client.post(
        f"/projects/{project_id}/intents/i001/release", json={"worker": "explorer"}
    ).status_code == 409

    resumed = claim_run(client, project_id, claim_id="run-2")
    assert resumed.status_code == 200
    assert resumed.json()["execution_mode"] == "session"
    assert resumed.json()["attempt_count"] == 1
    no_change = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-2",
            "session_id": "session-1",
            "checkpoint": {"progress": "different prose"},
            "evidence": {
                "artifacts": [artifact_a],
                "cursors": {"tested": 9},
                "milestones": [{"id": "scan-started", "artifact": artifact_a["path"]}],
            },
            "max_no_progress_slices": 3,
            "retry_after_seconds": 15,
        },
    )
    assert no_change.status_code == 200
    assert no_change.json()["no_progress_count"] == 1

    assert claim_run(client, project_id, claim_id="run-3").status_code == 200
    artifact_b = {"path": artifact_a["path"], "sha256": "b" * 64, "size": 11}
    progressed = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-3",
            "session_id": "session-1",
            "checkpoint": {"progress": "second"},
            "evidence": {
                "artifacts": [artifact_b],
                "cursors": {"tested": 11},
                "milestones": [],
            },
            "max_no_progress_slices": 3,
            "retry_after_seconds": 15,
        },
    )
    assert progressed.status_code == 200
    assert progressed.json()["no_progress_count"] == 0
    assert len(progressed.json()["evidence"]["artifacts"]) == 2

    # A -> B -> A 不会再次扩大累计证据。
    assert claim_run(client, project_id, claim_id="run-4").status_code == 200
    back_to_a = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-4",
            "session_id": "session-1",
            "checkpoint": {"progress": "third"},
            "evidence": {"artifacts": [artifact_a], "cursors": {"tested": 10}},
            "max_no_progress_slices": 3,
            "retry_after_seconds": 15,
        },
    )
    assert back_to_a.status_code == 200
    assert back_to_a.json()["no_progress_count"] == 1
    assert len(back_to_a.json()["evidence"]["artifacts"]) == 2


def test_checkpoint_only_and_failed_retry_attempts_are_persistent(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    yielded = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "session_id": "session-1",
            "checkpoint": {"progress": "saved"},
            "evidence": {"cursors": {"done": 1}},
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    )
    assert yielded.status_code == 200

    fallback = claim_run(
        client,
        project_id,
        claim_id="run-2",
        worker="other",
        worker_type="claudecode",
    )
    assert fallback.status_code == 200
    assert fallback.json()["execution_mode"] == "checkpoint_only"
    assert fallback.json()["attempt_count"] == 2
    assert fallback.json()["session_id"] is None
    assert fallback.json()["checkpoint"] == {"progress": "saved"}
    assert fallback.json()["evidence"]["cursors"] == {"done": 1}

    failed = client.post(
        run_url(project_id, "fail"),
        json={
            "worker": "other",
            "claim_id": "run-2",
            "session_id": "session-2",
            "error": "worker failed",
            "retry_after_seconds": 15,
        },
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["next_retry_at"] is not None
    assert client.post(
        f"/projects/{project_id}/intents/i001/heartbeat", json={"worker": "other"}
    ).status_code == 409
    assert claim_run(client, project_id, claim_id="run-3").status_code == 409

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intent_runs SET next_retry_at = '2000-01-01T00:00:00Z' "
            "WHERE project_id = ? AND intent_id = 'i001'",
            (project_id,),
        )
    retried = claim_run(client, project_id, claim_id="run-3")
    assert retried.status_code == 200
    assert retried.json()["execution_mode"] == "checkpoint_only"
    assert retried.json()["attempt_count"] == 3
    assert retried.json()["checkpoint"] == {"progress": "saved"}

    terminal = client.post(
        run_url(project_id, "fail"),
        json={
            "worker": "explorer",
            "claim_id": "run-3",
            "error": "attempts exhausted",
            "retry_after_seconds": None,
        },
    )
    assert terminal.status_code == 200
    assert terminal.json()["next_retry_at"] is None
    assert claim_run(client, project_id, claim_id="run-4").status_code == 409


def test_pi_session_resume_requires_the_same_worker_name(client: TestClient) -> None:
    project_id = create_active_project(client)
    assert claim_run(
        client,
        project_id,
        claim_id="run-1",
        worker="pi-a",
        worker_type="pi",
    ).status_code == 200
    yielded = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "pi-a",
            "claim_id": "run-1",
            "session_id": "pi-session",
            "checkpoint": {"progress": "saved"},
            "evidence": {"cursors": {"done": 1}},
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    )
    assert yielded.status_code == 200

    same_worker = claim_run(
        client,
        project_id,
        claim_id="run-2",
        worker="pi-a",
        worker_type="pi",
    )
    assert same_worker.status_code == 200
    assert same_worker.json()["execution_mode"] == "session"
    assert same_worker.json()["attempt_count"] == 1
    assert client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "pi-a",
            "claim_id": "run-2",
            "session_id": "pi-session",
            "checkpoint": {"progress": "saved again"},
            "evidence": {"cursors": {"done": 2}},
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    ).status_code == 200

    other_worker = claim_run(
        client,
        project_id,
        claim_id="run-3",
        worker="pi-b",
        worker_type="pi",
    )
    assert other_worker.status_code == 200
    assert other_worker.json()["execution_mode"] == "checkpoint_only"
    assert other_worker.json()["attempt_count"] == 2
    assert other_worker.json()["session_id"] is None
    assert other_worker.json()["checkpoint"] == {"progress": "saved again"}


def test_checkpoint_only_claim_retry_keeps_mode_without_a_checkpoint(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    failed = client.post(
        run_url(project_id, "fail"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "error": "failed before checkpoint",
            "retry_after_seconds": 1,
        },
    )
    assert failed.status_code == 200
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intent_runs SET next_retry_at = '2000-01-01T00:00:00Z' "
            "WHERE project_id = ? AND intent_id = 'i001'",
            (project_id,),
        )
    first = claim_run(client, project_id, claim_id="run-2")
    repeated = claim_run(client, project_id, claim_id="run-2")
    assert first.status_code == repeated.status_code == 200
    assert first.json()["execution_mode"] == "checkpoint_only"
    assert repeated.json()["execution_mode"] == "checkpoint_only"
    assert first.json()["attempt_count"] == repeated.json()["attempt_count"] == 2


def test_conclude_is_atomic_and_duplicate_cannot_create_another_fact(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    response = client.post(
        run_url(project_id, "conclude"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "session_id": "session-1",
            "description": "confirmed fact",
        },
    )
    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "confirmed fact"}
    repeated = client.post(
        run_url(project_id, "conclude"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "session_id": "session-1",
            "description": "duplicate",
        },
    )
    assert repeated.status_code == 409
    detail = client.get(f"/projects/{project_id}").json()
    assert len(detail["facts"]) == 3
    assert detail["intents"][0]["to"] == "f001"
    assert detail["intent_runs"][0]["status"] == "completed"
    assert client.post(
        f"/projects/{project_id}/intents/i001/release", json={"worker": "explorer"}
    ).status_code == 409


def test_heartbeat_read_expiry_stop_and_complete_keep_run_invariant(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    heartbeat = client.post(
        run_url(project_id, "heartbeat"),
        json={"worker": "explorer", "claim_id": "run-1"},
    )
    assert heartbeat.status_code == 200
    assert client.get("/projects").json()[0]["working_intent_count"] == 1
    assert client.get(f"/projects/{project_id}").json()["intent_runs"][0]["status"] == "running"
    assert client.get(f"/projects/{project_id}/export").status_code == 200

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' "
            "WHERE project_id = ? AND id = 'i001'",
            (project_id,),
        )
    expired = client.get(f"/projects/{project_id}").json()
    assert expired["intents"][0]["worker"] is None
    assert expired["intents"][0]["last_heartbeat_at"] is None
    assert expired["intent_runs"][0]["status"] == "yielded"
    assert client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "checkpoint": {},
            "evidence": {},
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    ).status_code == 409

    claim_run(client, project_id, claim_id="run-2")
    stopped = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert stopped.status_code == 200
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert detail["intent_runs"][0]["status"] == "yielded"
    assert client.post(
        run_url(project_id, "heartbeat"),
        json={"worker": "explorer", "claim_id": "run-2"},
    ).status_code in {403, 409}

    client.put(f"/projects/{project_id}/status", json={"status": "active"})
    claim_run(client, project_id, claim_id="run-3")
    completed = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "solved", "worker": "reasoner"},
    )
    assert completed.status_code == 200
    detail = client.get(f"/projects/{project_id}").json()
    open_intent = next(item for item in detail["intents"] if item["id"] == "i001")
    assert open_intent["worker"] is None
    assert detail["intent_runs"][0]["status"] == "yielded"
    assert client.post(
        run_url(project_id, "fail"),
        json={
            "worker": "explorer",
            "claim_id": "run-3",
            "error": "late",
            "retry_after_seconds": None,
        },
    ).status_code in {403, 409}


def test_expired_run_heartbeat_commits_lease_recovery_before_409(
    client: TestClient,
) -> None:
    project_id = create_active_project(client)
    assert claim_run(client, project_id, claim_id="run-1").status_code == 200
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' "
            "WHERE project_id = ? AND id = 'i001'",
            (project_id,),
        )

    response = client.post(
        run_url(project_id, "heartbeat"),
        json={"worker": "explorer", "claim_id": "run-1"},
    )

    assert response.status_code == 409
    # 不经过项目 GET 修复，mutation 自己就必须原子 yield Run 并清掉 Intent 租约。
    with db.get_conn() as conn:
        intent = conn.execute(
            "SELECT worker, last_heartbeat_at FROM intents "
            "WHERE project_id = ? AND id = 'i001'",
            (project_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT status, claim_id FROM intent_runs "
            "WHERE project_id = ? AND intent_id = 'i001'",
            (project_id,),
        ).fetchone()
    assert intent is not None
    assert run is not None
    assert (intent["worker"], intent["last_heartbeat_at"]) == (None, None)
    assert (run["status"], run["claim_id"]) == ("yielded", None)


def test_project_read_repairs_orphaned_running_run_invariant(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET worker = NULL, last_heartbeat_at = NULL "
            "WHERE project_id = ? AND id = 'i001'",
            (project_id,),
        )
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert detail["intent_runs"][0]["status"] == "yielded"


def test_no_progress_threshold_fails_without_creating_fact(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    first = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "checkpoint": {"progress": "only prose"},
            "evidence": {},
            "max_no_progress_slices": 1,
            "retry_after_seconds": None,
        },
    )
    assert first.status_code == 200
    assert first.json()["status"] == "failed"
    assert first.json()["no_progress_count"] == 1
    assert first.json()["next_retry_at"] is None
    detail = client.get(f"/projects/{project_id}").json()
    assert len(detail["facts"]) == 2
    assert detail["intents"][0]["to"] is None


def test_checkpoint_size_and_milestone_structure_are_rejected_atomically(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    oversized = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "checkpoint": {"data": "x" * 9000},
            "evidence": {},
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    )
    assert oversized.status_code == 422
    missing_artifact = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "checkpoint": {},
            "evidence": {"milestones": [{"id": "done", "artifact": "missing.json"}]},
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    )
    assert missing_artifact.status_code == 422
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] == "explorer"
    assert detail["intent_runs"][0]["status"] == "running"


def test_cumulative_evidence_limit_returns_422_instead_of_validation_500(client: TestClient) -> None:
    project_id = create_active_project(client)
    claim_run(client, project_id, claim_id="run-1")
    artifacts = [
        {
            "path": f"artifacts/item-{index}.json",
            "sha256": f"{index:064x}",
            "size": index,
        }
        for index in range(256)
    ]
    first = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-1",
            "checkpoint": {"progress": "many artifacts"},
            "evidence": {"artifacts": artifacts},
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    )
    assert first.status_code == 200
    assert claim_run(client, project_id, claim_id="run-2").status_code == 200
    overflow = client.post(
        run_url(project_id, "yield"),
        json={
            "worker": "explorer",
            "claim_id": "run-2",
            "checkpoint": {"progress": "one more"},
            "evidence": {
                "artifacts": [
                    {"path": "artifacts/overflow.json", "sha256": "f" * 64, "size": 1}
                ]
            },
            "max_no_progress_slices": 2,
            "retry_after_seconds": 15,
        },
    )
    assert overflow.status_code == 422
