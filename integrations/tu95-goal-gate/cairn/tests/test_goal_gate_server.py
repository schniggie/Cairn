from __future__ import annotations

import sqlite3

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


def create_project(client: TestClient, *, auto_activate: bool | None = None) -> dict:
    payload: dict = {
        "title": "review me",
        "origin": "starting point",
        "goal": "finish",
        "hints": [{"content": "initial clue", "creator": "human"}],
    }
    if auto_activate is not None:
        payload["auto_activate"] = auto_activate
    response = client.post("/projects", json=payload)
    assert response.status_code == 201
    return response.json()


def claim(client: TestClient, project_id: str, *, claim_id: str = "claim-1", revision: int = 1):
    return client.post(
        f"/projects/{project_id}/intake/claim",
        json={"worker": "reviewer", "claim_id": claim_id, "revision": revision},
    )


def ready_payload(
    *, claim_id: str = "claim-1", revision: int = 1, notices: list[str] | None = None
) -> dict:
    ready = {
        "origin": "normalized start",
        "goal": "normalized finish",
        "spec": {
            "objective": "finish",
            "success_criteria": ["done"],
            "resources": [],
            "constraints": [],
            "unknowns": [],
        },
    }
    if notices is not None:
        ready["notices"] = notices
    return {
        "worker": "reviewer",
        "claim_id": claim_id,
        "revision": revision,
        "ready": ready,
    }


def test_create_project_stays_preparing_and_holds_graph_inputs_in_intake(client: TestClient) -> None:
    payload = create_project(client)
    project_id = payload["project"]["id"]

    assert payload["project"]["status"] == "preparing"
    assert payload["facts"] == []
    assert payload["hints"] == []
    assert payload["intake"]["raw_origin"] == "starting point"
    assert payload["intake"]["raw_goal"] == "finish"
    assert payload["intent_runs"] == []
    assert "transcript" not in payload["intake"]

    summary = client.get("/projects").json()[0]
    assert summary["status"] == "preparing"
    assert summary["pending_question_count"] == 0

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "too soon", "creator": "reasoner"},
    ).status_code == 403
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "reasoner", "trigger": "bootstrap"},
    ).status_code == 403
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "too soon", "worker": "reasoner"},
    ).status_code == 403
    assert client.get(f"/projects/{project_id}/export").status_code == 409
    assert client.put(
        f"/projects/{project_id}/status", json={"status": "stopped"}
    ).status_code == 409


def test_export_commits_expired_intake_recovery_before_not_ready_409(
    client: TestClient,
) -> None:
    project_id = create_project(client)["project"]["id"]
    assert claim(client, project_id).status_code == 200
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE project_intakes SET review_last_heartbeat_at = '2000-01-01T00:00:00Z' "
            "WHERE project_id = ?",
            (project_id,),
        )

    response = client.get(f"/projects/{project_id}/export")

    assert response.status_code == 409
    # 不经过项目读接口，export 首次发现过期时就必须提交回收状态。
    with db.get_conn() as conn:
        intake = conn.execute(
            "SELECT attempt_count, review_worker, review_claim_id, "
            "review_last_heartbeat_at FROM project_intakes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    assert intake is not None
    assert intake["attempt_count"] == 1
    assert intake["review_worker"] is None
    assert intake["review_claim_id"] is None
    assert intake["review_last_heartbeat_at"] is None


def test_questions_answers_and_ready_activate_once(client: TestClient) -> None:
    project_id = create_project(client, auto_activate=True)["project"]["id"]
    response = claim(client, project_id)
    assert response.status_code == 200
    assert response.json()["raw_hints"] == [
        {"content": "initial clue", "creator": "human"}
    ]
    assert response.json()["transcript"] == []
    heartbeat = client.post(
        f"/projects/{project_id}/intake/heartbeat",
        json={"worker": "reviewer", "claim_id": "claim-1", "revision": 1},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["claim_id"] == "claim-1"

    response = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "claim-1",
            "revision": 1,
            "questions": [
                {
                    "key": "resources.target_url",
                    "question": "Target URL?",
                    "why_blocking": "No target",
                }
            ],
        },
    )
    assert response.status_code == 200
    question = response.json()["intake"]["pending_questions"][0]
    assert response.json()["project"]["status"] == "waiting_input"
    assert question["id"] == "q001_01"

    response = client.post(
        f"/projects/{project_id}/intake/answers",
        json={
            "revision": 1,
            "answers": [{"question_id": question["id"], "answer": "", "unknown": True}],
            "additional_context": "Use the provided test instance",
        },
    )
    assert response.status_code == 200
    assert response.json()["project"]["status"] == "preparing"
    assert response.json()["intake"]["revision"] == 2

    claimed = claim(client, project_id, claim_id="claim-2", revision=2)
    assert claimed.status_code == 200
    assert [item["type"] for item in claimed.json()["transcript"]] == ["answer", "context"]
    response = client.post(
        f"/projects/{project_id}/intake/decision",
        json=ready_payload(claim_id="claim-2", revision=2),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert [(fact["id"], fact["description"]) for fact in payload["facts"]] == [
        ("origin", "normalized start"),
        ("goal", "normalized finish"),
    ]
    assert len(payload["hints"]) == 1

    repeated = client.post(
        f"/projects/{project_id}/intake/decision",
        json=ready_payload(claim_id="claim-2", revision=2),
    )
    assert repeated.status_code == 200
    assert len(repeated.json()["facts"]) == 2
    assert len(repeated.json()["hints"]) == 1


def test_claim_id_revision_and_semantic_failure_are_enforced(client: TestClient) -> None:
    project_id = create_project(client)["project"]["id"]
    assert claim(client, project_id).status_code == 200
    assert claim(client, project_id).status_code == 200
    assert claim(client, project_id, claim_id="other").status_code == 409

    response = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "claim-1",
            "revision": 1,
            "questions": [
                {"key": "invalid.key", "question": "Bad?", "why_blocking": "Bad"}
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["project"]["status"] == "preparing"
    assert response.json()["intake"]["last_error"]
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT attempt_count, review_claim_id FROM project_intakes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    assert (row["attempt_count"], row["review_claim_id"]) == (1, None)

    assert claim(client, project_id, claim_id="claim-2", revision=1).status_code == 200
    stale = client.post(
        f"/projects/{project_id}/intake/heartbeat",
        json={"worker": "reviewer", "claim_id": "claim-1", "revision": 1},
    )
    assert stale.status_code == 409


def test_release_and_expired_leases_stop_after_configured_attempts(client: TestClient) -> None:
    project_id = create_project(client)["project"]["id"]
    client.put("/settings", json={"max_intake_attempts": 2})

    assert claim(client, project_id, claim_id="claim-1").status_code == 200
    response = client.post(
        f"/projects/{project_id}/intake/release",
        json={
            "worker": "reviewer",
            "claim_id": "claim-1",
            "revision": 1,
            "error": "invalid model output",
        },
    )
    assert response.status_code == 200
    assert response.json()["project"]["status"] == "preparing"

    assert claim(client, project_id, claim_id="claim-2").status_code == 200
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE project_intakes SET review_last_heartbeat_at = '2000-01-01T00:00:00Z' "
            "WHERE project_id = ?",
            (project_id,),
        )
    response = client.get(f"/projects/{project_id}")
    assert response.json()["project"]["status"] == "intake_failed"
    assert response.json()["intake"]["pending_questions"] == []

    retry = client.post(f"/projects/{project_id}/intake/retry")
    assert retry.status_code == 200
    assert retry.json()["project"]["status"] == "preparing"
    assert retry.json()["intake"]["revision"] == 2
    assert client.post(f"/projects/{project_id}/intake/retry").status_code == 409
    assert claim(client, project_id, claim_id="claim-2", revision=1).status_code == 409


def test_expired_intake_heartbeat_commits_lease_recovery_before_409(
    client: TestClient,
) -> None:
    project_id = create_project(client)["project"]["id"]
    assert claim(client, project_id, claim_id="claim-1").status_code == 200
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE project_intakes SET review_last_heartbeat_at = '2000-01-01T00:00:00Z' "
            "WHERE project_id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intake/heartbeat",
        json={"worker": "reviewer", "claim_id": "claim-1", "revision": 1},
    )

    assert response.status_code == 409
    # 不经过项目 GET 修复，mutation 自己就必须把过期回收提交到库。
    with db.get_conn() as conn:
        intake = conn.execute(
            "SELECT attempt_count, review_worker, review_claim_id, "
            "review_last_heartbeat_at FROM project_intakes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    assert intake is not None
    assert intake["attempt_count"] == 1
    assert intake["review_worker"] is None
    assert intake["review_claim_id"] is None
    assert intake["review_last_heartbeat_at"] is None


def test_failed_context_only_and_force_activation(client: TestClient) -> None:
    project_id = create_project(client)["project"]["id"]
    client.put("/settings", json={"max_intake_attempts": 1})
    assert claim(client, project_id).status_code == 200
    client.post(
        f"/projects/{project_id}/intake/release",
        json={
            "worker": "reviewer",
            "claim_id": "claim-1",
            "revision": 1,
            "error": "failed",
        },
    )

    assert client.post(
        f"/projects/{project_id}/intake/answers",
        json={"revision": 1, "answers": [], "additional_context": ""},
    ).status_code == 422
    response = client.post(
        f"/projects/{project_id}/intake/answers",
        json={"revision": 1, "answers": [], "additional_context": "new access details"},
    )
    assert response.status_code == 200
    assert response.json()["intake"]["revision"] == 2

    # Force 只允许 waiting_input/intake_failed，preparing 必须拒绝。
    assert client.post(
        f"/projects/{project_id}/intake/force", json={"confirm": True}
    ).status_code == 409

    assert claim(client, project_id, claim_id="claim-2", revision=2).status_code == 200
    client.post(
        f"/projects/{project_id}/intake/release",
        json={
            "worker": "reviewer",
            "claim_id": "claim-2",
            "revision": 2,
            "error": "failed again",
        },
    )
    assert client.post(
        f"/projects/{project_id}/intake/force", json={"confirm": False}
    ).status_code == 422
    forced = client.post(
        f"/projects/{project_id}/intake/force", json={"confirm": True}
    )
    assert forced.status_code == 200
    assert forced.json()["project"]["status"] == "active"
    assert forced.json()["intake"]["activation_mode"] == "forced"
    assert "new access details" in forced.json()["facts"][0]["description"]
    assert len(forced.json()["hints"]) == 1
    repeated = client.post(
        f"/projects/{project_id}/intake/force", json={"confirm": True}
    )
    assert repeated.status_code == 200
    assert len(repeated.json()["facts"]) == 2
    assert len(repeated.json()["hints"]) == 1


def test_round_limit_does_not_save_overflow_questions(client: TestClient) -> None:
    project_id = create_project(client)["project"]["id"]
    client.put("/settings", json={"max_intake_clarification_rounds": 1})
    assert claim(client, project_id).status_code == 200
    first = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "claim-1",
            "revision": 1,
            "questions": [
                {"key": "resources.target", "question": "Target?", "why_blocking": "Needed"}
            ],
        },
    ).json()
    question_id = first["intake"]["pending_questions"][0]["id"]
    answered = client.post(
        f"/projects/{project_id}/intake/answers",
        json={
            "revision": 1,
            "answers": [{"question_id": question_id, "answer": "x", "unknown": False}],
        },
    ).json()
    assert answered["intake"]["revision"] == 2

    assert claim(client, project_id, claim_id="claim-2", revision=2).status_code == 200
    overflow = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "claim-2",
            "revision": 2,
            "questions": [
                {"key": "success.report", "question": "Report?", "why_blocking": "Needed"}
            ],
        },
    )
    assert overflow.status_code == 200
    assert overflow.json()["project"]["status"] == "intake_failed"
    assert overflow.json()["intake"]["pending_questions"] == []


def test_answered_question_key_cannot_be_asked_again(client: TestClient) -> None:
    project_id = create_project(client)["project"]["id"]
    claim(client, project_id)
    first = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "claim-1",
            "revision": 1,
            "questions": [
                {
                    "key": "resources.target",
                    "question": "Target?",
                    "why_blocking": "Needed",
                }
            ],
        },
    ).json()
    question_id = first["intake"]["pending_questions"][0]["id"]
    answered = client.post(
        f"/projects/{project_id}/intake/answers",
        json={
            "revision": 1,
            "answers": [{"question_id": question_id, "answer": "", "unknown": True}],
        },
    ).json()
    assert answered["intake"]["revision"] == 2
    claim(client, project_id, claim_id="claim-2", revision=2)
    repeated = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "claim-2",
            "revision": 2,
            "questions": [
                {
                    "key": "resources.target",
                    "question": "Target again?",
                    "why_blocking": "Still needed",
                }
            ],
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["project"]["status"] == "preparing"
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT attempt_count, review_claim_id FROM project_intakes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    assert (row["attempt_count"], row["review_claim_id"]) == (1, None)


def test_structurally_invalid_question_count_is_422_and_not_an_attempt(client: TestClient) -> None:
    project_id = create_project(client)["project"]["id"]
    claim(client, project_id)
    response = client.post(
        f"/projects/{project_id}/intake/decision",
        json={
            "worker": "reviewer",
            "claim_id": "claim-1",
            "revision": 1,
            "questions": [],
        },
    )
    assert response.status_code == 422
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT attempt_count, review_claim_id FROM project_intakes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    assert (row["attempt_count"], row["review_claim_id"]) == (0, "claim-1")


def enter_ready_review(client: TestClient, *, notices: list[str] | None = None) -> str:
    project_id = create_project(client)["project"]["id"]
    assert claim(client, project_id).status_code == 200
    response = client.post(
        f"/projects/{project_id}/intake/decision",
        json=ready_payload(notices=notices),
    )
    assert response.status_code == 200
    assert response.json()["project"]["status"] == "ready_review"
    return project_id


def test_ready_review_holds_proposal_until_user_confirms(client: TestClient) -> None:
    project_id = create_project(client)["project"]["id"]

    # preparing 下 confirm 被拒绝。
    assert client.post(
        f"/projects/{project_id}/intake/confirm", json={"revision": 1}
    ).status_code == 409

    assert claim(client, project_id).status_code == 200
    response = client.post(
        f"/projects/{project_id}/intake/decision",
        json=ready_payload(notices=["origin 已按授权范围改写", "目标有效期仍不确定"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "ready_review"
    assert payload["facts"] == []
    assert payload["hints"] == []
    intake = payload["intake"]
    assert intake["pending_proposal"]["origin"] == "normalized start"
    assert intake["pending_proposal"]["goal"] == "normalized finish"
    assert intake["pending_proposal"]["spec"]["objective"] == "finish"
    assert intake["pending_proposal"]["notices"] == [
        "origin 已按授权范围改写",
        "目标有效期仍不确定",
    ]
    assert intake["auto_activate"] is None

    # 同一 decision 重试幂等：不重复写 proposal，也不写知识图。
    repeated = client.post(
        f"/projects/{project_id}/intake/decision", json=ready_payload()
    )
    assert repeated.status_code == 200
    assert repeated.json()["project"]["status"] == "ready_review"
    assert repeated.json()["facts"] == []

    # ready_review 与其他 intake 状态一样禁止图写接口、直接改状态与导出。
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "too soon", "creator": "reasoner"},
    ).status_code == 403
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "reasoner", "trigger": "bootstrap"},
    ).status_code == 403
    assert client.get(f"/projects/{project_id}/export").status_code == 409
    assert client.put(
        f"/projects/{project_id}/status", json={"status": "stopped"}
    ).status_code == 409

    # 状态与 revision 串行化：陈旧 revision 返回 409。
    assert client.post(
        f"/projects/{project_id}/intake/confirm", json={"revision": 2}
    ).status_code == 409

    confirmed = client.post(
        f"/projects/{project_id}/intake/confirm", json={"revision": 1}
    )
    assert confirmed.status_code == 200
    detail = confirmed.json()
    assert detail["project"]["status"] == "active"
    assert [(fact["id"], fact["description"]) for fact in detail["facts"]] == [
        ("origin", "normalized start"),
        ("goal", "normalized finish"),
    ]
    assert len(detail["hints"]) == 1
    assert detail["intake"]["activation_mode"] == "gate"
    assert detail["intake"]["pending_proposal"] is None

    # 重复 confirm 幂等返回当前项目，不重复插入。
    again = client.post(
        f"/projects/{project_id}/intake/confirm", json={"revision": 1}
    )
    assert again.status_code == 200
    assert len(again.json()["facts"]) == 2
    assert len(again.json()["hints"]) == 1


def test_ready_decision_activates_directly_with_project_auto_activate(
    client: TestClient,
) -> None:
    project_id = create_project(client, auto_activate=True)["project"]["id"]
    assert claim(client, project_id).status_code == 200
    response = client.post(
        f"/projects/{project_id}/intake/decision", json=ready_payload()
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert [fact["id"] for fact in payload["facts"]] == ["origin", "goal"]
    assert payload["intake"]["activation_mode"] == "gate"
    assert payload["intake"]["pending_proposal"] is None
    assert payload["intake"]["auto_activate"] is True


def test_project_auto_activate_overrides_server_setting(client: TestClient) -> None:
    assert client.put(
        "/settings", json={"intake_auto_activate": True}
    ).status_code == 200

    following = create_project(client)["project"]["id"]
    assert claim(client, following).status_code == 200
    response = client.post(
        f"/projects/{following}/intake/decision", json=ready_payload()
    )
    assert response.json()["project"]["status"] == "active"

    overridden = create_project(client, auto_activate=False)["project"]["id"]
    assert claim(client, overridden).status_code == 200
    response = client.post(
        f"/projects/{overridden}/intake/decision", json=ready_payload()
    )
    assert response.json()["project"]["status"] == "ready_review"


def test_intake_auto_activate_setting_defaults_false_and_survives_partial_updates(
    client: TestClient,
) -> None:
    assert client.get("/settings").json()["intake_auto_activate"] is False
    updated = client.put("/settings", json={"intake_auto_activate": True})
    assert updated.status_code == 200
    assert updated.json()["intake_auto_activate"] is True
    unrelated = client.put("/settings", json={"intent_timeout": 20})
    assert unrelated.status_code == 200
    assert unrelated.json()["intake_auto_activate"] is True


def test_ready_review_edit_submission_replaces_raw_and_requeues_review(
    client: TestClient,
) -> None:
    project_id = enter_ready_review(client)

    edited_origin = "edited start " + "x" * 200
    edited_goal = "edited finish"
    response = client.post(
        f"/projects/{project_id}/intake/answers",
        json={
            "revision": 1,
            "answers": [],
            "edited_origin": edited_origin,
            "edited_goal": edited_goal,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "preparing"
    assert payload["intake"]["revision"] == 2
    assert payload["intake"]["raw_origin"] == edited_origin
    assert payload["intake"]["raw_goal"] == edited_goal
    assert payload["intake"]["pending_proposal"] is None

    # 编辑事件只记长度与预览，不保存全文。
    claimed = claim(client, project_id, claim_id="claim-2", revision=2)
    assert claimed.status_code == 200
    edit_entry = claimed.json()["transcript"][-1]
    assert edit_entry["type"] == "edit"
    assert edit_entry["origin_length"] == len(edited_origin)
    assert edit_entry["goal_length"] == len(edited_goal)
    assert edit_entry["origin_preview"] == edited_origin[:120]
    assert edited_origin not in edit_entry["origin_preview"]

    # 重新审查产生新 proposal，confirm 后激活；迟到的编辑提交被状态门禁拒绝。
    decision = client.post(
        f"/projects/{project_id}/intake/decision",
        json=ready_payload(claim_id="claim-2", revision=2),
    )
    assert decision.json()["project"]["status"] == "ready_review"
    confirmed = client.post(
        f"/projects/{project_id}/intake/confirm", json={"revision": 2}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["project"]["status"] == "active"
    late_edit = client.post(
        f"/projects/{project_id}/intake/answers",
        json={
            "revision": 2,
            "answers": [],
            "edited_origin": "too late",
            "edited_goal": "too late",
        },
    )
    assert late_edit.status_code == 409


def test_ready_review_edit_submission_rejects_invalid_payloads(client: TestClient) -> None:
    project_id = enter_ready_review(client)

    url = f"/projects/{project_id}/intake/answers"
    assert client.post(
        url,
        json={"revision": 1, "answers": [], "edited_origin": "", "edited_goal": "x"},
    ).status_code == 422
    assert client.post(
        url, json={"revision": 1, "answers": [], "edited_origin": "x"}
    ).status_code == 422
    assert client.post(
        url,
        json={
            "revision": 1,
            "answers": [{"question_id": "q001_01", "answer": "x", "unknown": False}],
            "edited_origin": "x",
            "edited_goal": "y",
        },
    ).status_code == 422
    assert client.post(
        url,
        json={"revision": 2, "answers": [], "edited_origin": "x", "edited_goal": "y"},
    ).status_code == 409

    # 全部被拒绝后仍处于 ready_review，proposal 未受影响。
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "ready_review"
    assert detail["intake"]["pending_proposal"]["origin"] == "normalized start"


def test_ready_review_force_uses_raw_input_and_clears_proposal(client: TestClient) -> None:
    project_id = enter_ready_review(client)

    forced = client.post(f"/projects/{project_id}/intake/force", json={"confirm": True})
    assert forced.status_code == 200
    payload = forced.json()
    assert payload["project"]["status"] == "active"
    assert payload["intake"]["activation_mode"] == "forced"
    assert payload["intake"]["pending_proposal"] is None
    # force 放弃 proposal，按创建时的 raw 输入激活。
    assert [(fact["id"], fact["description"]) for fact in payload["facts"]] == [
        ("origin", "starting point"),
        ("goal", "finish"),
    ]
    assert len(payload["hints"]) == 1


def test_partial_settings_update_preserves_intake_limits(client: TestClient) -> None:
    updated = client.put(
        "/settings",
        json={
            "intake_lease_timeout": 30,
            "max_intake_attempts": 4,
            "max_intake_clarification_rounds": 5,
        },
    )
    assert updated.status_code == 200
    old_frontend_update = client.put(
        "/settings", json={"intent_timeout": 25, "reason_timeout": 35}
    )
    assert old_frontend_update.status_code == 200
    assert old_frontend_update.json() == {
        "intent_timeout": 25,
        "reason_timeout": 35,
        "intake_lease_timeout": 30,
        "max_intake_attempts": 4,
        "max_intake_clarification_rounds": 5,
        "intake_auto_activate": False,
    }


def test_legacy_settings_migration_adds_intake_columns_and_is_repeatable(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "legacy-settings.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE settings (intent_timeout INTEGER NOT NULL, reason_timeout INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO settings VALUES (21, 34)")

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE rowid = 1").fetchone()
        assert (row["intent_timeout"], row["reason_timeout"]) == (21, 34)
        assert (
            row["intake_lease_timeout"],
            row["max_intake_attempts"],
            row["max_intake_clarification_rounds"],
            row["intake_auto_activate"],
        ) == (15, 3, 3, 0)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_intakes'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'intent_runs'"
        ).fetchone()

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE rowid = 1").fetchone()
    assert (row["intent_timeout"], row["reason_timeout"]) == (21, 34)


def test_project_max_workers_default_create_bounds_and_update(client: TestClient) -> None:
    payload = create_project(client)
    project_id = payload["project"]["id"]

    # 默认 8；ProjectMeta 与 ProjectSummary 都携带。
    assert payload["project"]["max_workers"] == 8
    summary = client.get("/projects").json()[0]
    assert summary["max_workers"] == 8

    created = client.post(
        "/projects",
        json={"title": "t", "origin": "o", "goal": "g", "max_workers": 3},
    )
    assert created.status_code == 201
    assert created.json()["project"]["max_workers"] == 3

    for bad in (0, 9, -1, "many", 2.5):
        assert client.post(
            "/projects",
            json={"title": "t", "origin": "o", "goal": "g", "max_workers": bad},
        ).status_code == 422

    # 任何状态都允许修改（该项目仍处于 preparing）；1 和 8 接受。
    for good in (1, 8):
        updated = client.put(f"/projects/{project_id}/max_workers", json={"max_workers": good})
        assert updated.status_code == 200
        assert updated.json()["max_workers"] == good

    for bad in (0, 9, -1, "many", 2.5):
        assert client.put(
            f"/projects/{project_id}/max_workers", json={"max_workers": bad}
        ).status_code == 422

    assert client.get(f"/projects/{project_id}").json()["project"]["max_workers"] == 8
    assert client.put("/projects/proj_404/max_workers", json={"max_workers": 2}).status_code == 404
