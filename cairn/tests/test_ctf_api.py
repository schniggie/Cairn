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


def _make_challenge(**overrides) -> dict:
    payload = {
        "external_id": "ch-1",
        "title": "Warmup",
        "category": "misc",
        "points": 100,
        "description": "Find the flag.",
        "target": "10.0.0.1:8080",
        "attachments": ["http://example/files/a"],
        "hints": ["hint one"],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- config


def test_config_defaults_and_masking(client: TestClient) -> None:
    response = client.get("/ctf/config")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "manual"
    assert body["adapter"] == "ctfd"
    assert body["base_url"] == ""
    assert body["flag_regex"] == "(?:DASCTF|flag)\\{[^}]+\\}"
    assert body["auto_submit"] is True
    assert body["max_concurrent"] == 2
    assert body["poll_interval"] == 10
    assert body["token"] == ""
    assert body["model_api_key"] == ""


def test_config_put_and_token_masking(client: TestClient) -> None:
    response = client.put(
        "/ctf/config",
        json={"base_url": "https://ctf.example", "token": "sekrit", "max_concurrent": 3},
    )
    assert response.status_code == 200
    assert response.json()["base_url"] == "https://ctf.example"
    assert response.json()["token"] == "***"
    assert response.json()["max_concurrent"] == 3

    # masked token echoed back on save must not overwrite the real token
    client.put("/ctf/config", json={"token": "***", "team_name": "team-a"})
    full = client.get("/ctf/config?full=true").json()
    assert full["token"] == "sekrit"
    assert full["team_name"] == "team-a"

    # explicit empty token clears it
    client.put("/ctf/config", json={"token": ""})
    assert client.get("/ctf/config?full=true").json()["token"] == ""


def test_config_rejects_invalid_flag_regex(client: TestClient) -> None:
    response = client.put("/ctf/config", json={"flag_regex": "flag(?"})
    assert response.status_code == 400


def test_mode_switch(client: TestClient) -> None:
    assert client.put("/ctf/mode", json={"mode": "ctf"}).json()["mode"] == "ctf"
    assert client.put("/ctf/mode", json={"mode": "manual"}).json()["mode"] == "manual"
    assert client.put("/ctf/mode", json={"mode": "bogus"}).status_code == 422


# ------------------------------------------------------------ challenges


def test_challenge_create_list_update(client: TestClient) -> None:
    created = client.post("/ctf/challenges", json=_make_challenge())
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "queued"
    assert body["attachments"] == ["http://example/files/a"]
    assert body["hints"] == ["hint one"]

    # duplicate external_id -> 409
    assert client.post("/ctf/challenges", json=_make_challenge()).status_code == 409

    listed = client.get("/ctf/challenges").json()
    assert len(listed) == 1
    assert listed[0]["external_id"] == "ch-1"

    updated = client.put(
        f"/ctf/challenges/{body['id']}",
        json={"status": "solving", "project_id": "proj_001", "points": 150},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "solving"
    assert updated.json()["project_id"] == "proj_001"
    assert updated.json()["points"] == 150


def test_challenge_validation(client: TestClient) -> None:
    assert client.post("/ctf/challenges", json={"title": "no id"}).status_code == 400
    assert client.post("/ctf/challenges", json=_make_challenge(status="bogus")).status_code == 400
    assert client.put("/ctf/challenges/999", json={"status": "solved"}).status_code == 404
    created = client.post("/ctf/challenges", json=_make_challenge()).json()
    assert client.put(f"/ctf/challenges/{created['id']}", json={"status": "nope"}).status_code == 400


# ---------------------------------------------------------------- status


def test_status_counts(client: TestClient) -> None:
    client.put("/ctf/config", json={"base_url": "http://mock"})
    client.post("/ctf/challenges", json=_make_challenge(external_id="a", points=10))
    client.post("/ctf/challenges", json=_make_challenge(external_id="b", points=20))
    b = client.get("/ctf/challenges").json()
    for row in b:
        if row["external_id"] == "a":
            client.put(f"/ctf/challenges/{row['id']}", json={"status": "solved"})

    status = client.get("/ctf/status").json()
    assert status["mode"] == "manual"
    assert status["connected"] is True
    assert status["bridge_running"] is False
    assert status["counts"]["queued"] == 1
    assert status["counts"]["solved"] == 1
    assert status["total"] == 2
    assert status["total_points_solved"] == 10


def test_sync_and_heartbeat(client: TestClient) -> None:
    client.post("/ctf/sync")
    assert client.get("/ctf/config").json()["sync_requested"] is True

    client.post("/ctf/sync/ack", json={"last_sync_at": "2026-01-01T00:00:00Z"})
    config = client.get("/ctf/config").json()
    assert config["sync_requested"] is False
    assert config["last_sync_at"] == "2026-01-01T00:00:00Z"

    client.post("/ctf/heartbeat", json={"error": "boom"})
    config = client.get("/ctf/config?full=true").json()
    assert config["bridge_error"] == "boom"
    assert config["bridge_heartbeat_at"] is not None

    client.post("/ctf/heartbeat", json={"error": None})
    assert client.get("/ctf/config").json()["bridge_error"] is None


# ---------------------------------------------------------- test + submit


def test_connection_probe_with_mock_adapter(client: TestClient) -> None:
    client.put("/ctf/config", json={"adapter": "mock", "base_url": "http://mock.invalid"})
    response = client.post("/ctf/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    client.put("/ctf/config", json={"base_url": ""})
    response = client.post("/ctf/test")
    assert response.json()["ok"] is False


# --------------------------------------------------------- new config fields


def test_config_extra_fields_and_model_secret_masking(client: TestClient) -> None:
    response = client.put(
        "/ctf/config",
        json={
            "env_poll_interval": 7,
            "env_timeout": 240,
            "submission_max_retries": 3,
            "rate_limit_backoff": 60,
            "model_base_url": "https://llm.example/apps/anthropic",
            "model_name": "deepseek-v4-pro-0813",
            "model_api_key": "sk-sekrit-model",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["env_poll_interval"] == 7
    assert body["env_timeout"] == 240
    assert body["submission_max_retries"] == 3
    assert body["rate_limit_backoff"] == 60
    assert body["model_base_url"] == "https://llm.example/apps/anthropic"
    assert body["model_name"] == "deepseek-v4-pro-0813"
    assert body["model_api_key"] == "***"

    full = client.get("/ctf/config?full=true").json()
    assert full["model_api_key"] == "sk-sekrit-model"

    # masked model key echoed back must not clobber the real one
    client.put("/ctf/config", json={"model_api_key": "***"})
    assert client.get("/ctf/config?full=true").json()["model_api_key"] == "sk-sekrit-model"


def test_heartbeat_reports_model_health(client: TestClient) -> None:
    client.post("/ctf/heartbeat", json={"model_ok": False, "model_error": "Free quota exhausted"})
    status = client.get("/ctf/status").json()
    assert status["model_health_at"] is not None
    assert status["last_model_error"] == "Free quota exhausted"
    assert status["model_name"] == ""

    client.post("/ctf/heartbeat", json={"model_ok": True})
    assert client.get("/ctf/status").json()["last_model_error"] is None


# ------------------------------------------------------------- lifecycle


def test_challenge_lifecycle_retry_pause_resume_stop(client: TestClient) -> None:
    created = client.post("/ctf/challenges", json=_make_challenge()).json()

    # pause a solving challenge -> paused, project cleared
    client.put(f"/ctf/challenges/{created['id']}", json={"status": "solving", "project_id": "proj_x"})
    paused = client.post(f"/ctf/challenges/{created['id']}/pause").json()
    assert paused["status"] == "paused"
    assert paused["project_id"] is None

    # resume -> queued
    resumed = client.post(f"/ctf/challenges/{created['id']}/resume").json()
    assert resumed["status"] == "queued"

    # retry from a failed challenge -> queued
    client.put(f"/ctf/challenges/{created['id']}", json={"status": "failed", "attempt_count": 3})
    retried = client.post(f"/ctf/challenges/{created['id']}/retry").json()
    assert retried["status"] == "queued"
    assert retried["attempt_count"] == 3  # history preserved

    # stop -> failed
    client.put(f"/ctf/challenges/{created['id']}", json={"status": "solving", "project_id": "proj_y"})
    stopped = client.post(f"/ctf/challenges/{created['id']}/stop").json()
    assert stopped["status"] == "failed"
    assert stopped["project_id"] is None

    # a solved challenge cannot be stopped / paused
    client.put(f"/ctf/challenges/{created['id']}", json={"status": "solved"})
    assert client.post(f"/ctf/challenges/{created['id']}/stop").status_code == 409
    assert client.post(f"/ctf/challenges/{created['id']}/pause").status_code == 409
    # ...but it cannot be retried either (not in retryable set)
    assert client.post(f"/ctf/challenges/{created['id']}/retry").status_code == 409


def test_manual_submit_with_mock_adapter(client: TestClient) -> None:
    client.put("/ctf/config", json={"adapter": "mock", "base_url": "http://mock.invalid"})
    created = client.post("/ctf/challenges", json=_make_challenge(external_id="mock-hello")).json()

    # correct flag
    response = client.post(
        "/ctf/submit", json={"challenge_id": str(created["id"]), "flag": "flag{hello}"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status"] == "solved"

    # wrong flag on a fresh challenge
    created = client.post("/ctf/challenges", json=_make_challenge(external_id="mock-trap")).json()
    response = client.post(
        "/ctf/submit", json={"challenge_id": created["external_id"], "flag": "flag{nope}"}
    )
    assert response.json()["ok"] is False
    assert response.json()["status"] == "wrong_flag"
