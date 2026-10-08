from __future__ import annotations

import json

from fastapi.testclient import TestClient

from cairn.dispatcher.prompting import format_project_knowledge
from cairn.dispatcher.runtime import engine_resolve
from cairn.dispatcher.workers.adapters.opencode import OpenCodeDriver
from cairn.server import db
from cairn.server.app import app

from conftest import make_config


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    return TestClient(app)


def test_project_records_backend_and_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("cairn.server.app.ADMIN_TOKEN", "test-admin")
    headers = {"Authorization": "Bearer test-admin"}
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/projects",
            headers=headers,
            json={
                "title": "local",
                "origin": "start",
                "goal": "finish",
                "backend": "local",
                "project_root": "/tmp/sample-root",
            },
        )
        assert created.status_code == 201
        project = created.json()["project"]
        assert project["backend"] == "local"
        assert project["project_root"] == "/tmp/sample-root"
        listed = client.get("/projects", headers=headers).json()[0]
        assert listed["backend"] == "local"


def test_skills_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("cairn.server.app.ADMIN_TOKEN", "test-admin")
    headers = {"Authorization": "Bearer test-admin"}
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/skills",
            headers=headers,
            json={"name": "recon-scan", "content": "---\nname: recon-scan\ndescription: scan\n---\n# Scan\n"},
        )
        assert created.status_code == 201
        assert created.json()["description"] == "scan"
        listed = client.get("/skills", headers=headers).json()
        assert listed[0]["name"] == "recon-scan"
        assert client.delete("/skills/recon-scan", headers=headers).status_code == 200
        assert client.get("/skills", headers=headers).json() == []


def test_opencode_uses_provider_wrapper_only_when_configured() -> None:
    payload = make_config().model_dump()
    payload["runtime"]["execution"] = "local"
    payload["local"] = {}
    payload["workers"][0]["type"] = "opencode"
    payload["workers"][0]["env"] = {}
    worker = type(make_config()).model_validate(payload).workers[0]
    bare = OpenCodeDriver().build_execute(worker, "ping", None)
    assert bare.argv[0] == "opencode"
    assert "--" in bare.argv

    payload["runtime"]["execution"] = "container"
    payload.pop("local", None)
    payload["workers"][0]["env"] = {
        "OPENCODE_MODEL": "demo",
        "OPENCODE_BASE_URL": "http://127.0.0.1:9/v1",
        "OPENCODE_API_KEY": "token",
    }
    worker = type(make_config()).model_validate(payload).workers[0]
    wrapped = OpenCodeDriver().build_execute(worker, "ping", "ses_1")
    assert wrapped.argv[0] == "/bin/sh"
    assert "cairn/demo" in wrapped.argv
    assert "-s" in wrapped.argv


def test_project_knowledge_lists_present_subdirs(tmp_path) -> None:
    (tmp_path / "docs-out").mkdir()
    text = format_project_knowledge(str(tmp_path), ["docs-out"])
    assert "docs-out" in text
    assert format_project_knowledge(None, ["docs-out"]) == ""


def test_engine_override_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path / "home"))
    engine_resolve.set_override("opencode", "/usr/local/bin/opencode", "direct")
    stored = json.loads((tmp_path / "home" / "engines.json").read_text(encoding="utf-8"))
    assert stored["opencode"]["path"] == "/usr/local/bin/opencode"
    engine_resolve.remove_override("opencode")
    assert engine_resolve.load_overrides() == {}
