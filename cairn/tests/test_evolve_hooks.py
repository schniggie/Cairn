from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.responses import PlainTextResponse

from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.server import db
from cairn.server.app import ADMIN_TOKEN, AdminTokenMiddleware, app
from cairn.server import app as app_module

from conftest import make_config


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def test_create_project_persists_init_files(client: TestClient) -> None:
    created = client.post(
        "/projects",
        json={
            "title": "seeded",
            "origin": "start",
            "goal": "finish",
            "init_files": [
                {"path": "/workspace/notes.txt", "content": "hello"},
                {"path": "/workspace/blob.bin", "content": "aGVsbG8=", "encoding": "base64"},
            ],
        },
    )
    assert created.status_code == 201
    files = created.json()["init_files"]
    assert files[0]["id"] == "file_001"
    assert files[0]["path"] == "/workspace/notes.txt"
    assert files[1]["encoding"] == "base64"

    project_id = created.json()["project"]["id"]
    loaded = client.get(f"/projects/{project_id}")
    assert loaded.status_code == 200
    assert [item["path"] for item in loaded.json()["init_files"]] == [
        "/workspace/notes.txt",
        "/workspace/blob.bin",
    ]


def test_worker_model_override_is_optional() -> None:
    payload = make_config().model_dump()
    payload["workers"][0]["type"] = "claudecode"
    payload["workers"][0]["model"] = "claude-opus"
    payload["workers"][0]["env"] = {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_AUTH_TOKEN": "token",
        "ANTHROPIC_MODEL": "default-model",
    }
    config = type(make_config()).model_validate(payload)
    worker = config.workers[0]
    argv = ClaudeCodeDriver().build_execute(worker, "prompt", "session-1").argv
    assert argv[:3] == ["claude", "--model", "claude-opus"]

    payload["workers"][0]["model"] = "   "
    with pytest.raises(ValidationError, match="model must not be empty"):
        type(make_config()).model_validate(payload)


def test_admin_token_guards_project_reads_and_allows_worker_writes(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "ADMIN_TOKEN", "secret")
    assert ADMIN_TOKEN == "" or isinstance(ADMIN_TOKEN, str)

    guarded = FastAPI()

    @guarded.get("/projects")
    def list_projects():
        return PlainTextResponse("ok")

    @guarded.post("/projects/p001/heartbeat")
    def heartbeat():
        return PlainTextResponse("beat")

    @guarded.post("/projects/p001/fail")
    def fail():
        return PlainTextResponse("failed")

    guarded.add_middleware(AdminTokenMiddleware)
    with TestClient(guarded) as test_client:
        assert test_client.get("/projects").status_code == 403
        assert test_client.get("/projects", headers={"Authorization": "Bearer secret"}).status_code == 200
        assert test_client.post("/projects/p001/heartbeat").status_code == 200
        assert test_client.post("/projects/p001/fail").status_code == 200


def test_claude_and_codex_extract_tool_trajectories() -> None:
    claude_log = "\n".join(
        [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "look around"},
                            {"type": "tool_use", "id": "call-1", "name": "bash", "input": {"command": "ls"}},
                        ],
                    }
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "README.md",
                    }
                }
            ),
        ]
    )
    steps = ClaudeCodeDriver().extract_trajectory(claude_log)
    assert len(steps) == 1
    assert steps[0].action == "ls"
    assert steps[0].observation == "README.md"
    assert steps[0].tool_type == "bash"

    codex_log = "\n".join(
        [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "shell", "arguments": json.dumps({"command": "id"})},
                            }
                        ],
                    }
                }
            ),
            json.dumps({"message": {"role": "tool", "tool_call_id": "c1", "content": "uid=0"}}),
        ]
    )
    codex_steps = CodexDriver().extract_trajectory(codex_log)
    assert codex_steps[0].action == "id"
    assert codex_steps[0].observation == "uid=0"
    assert codex_steps[0].tool_type == "shell"
