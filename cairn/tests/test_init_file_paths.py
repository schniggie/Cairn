"""Init files stay inside the project workspace, and host execution needs an admin token."""

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.workspace_files import (
    InitFilePathError,
    container_init_destination,
    local_init_destination,
)
from cairn.dispatcher.config import LocalConfig
from cairn.server import db
from cairn.server.app import app


def test_init_paths_reject_escape_and_symlink(tmp_path: Path) -> None:
    assert container_init_destination("/workspace/notes.txt") == "/workspace/notes.txt"
    assert container_init_destination("notes.txt") == "/workspace/notes.txt"
    with pytest.raises(InitFilePathError):
        container_init_destination("/cairn/dispatch.yaml")
    with pytest.raises(InitFilePathError):
        container_init_destination("/workspace/../../etc/passwd")
    with pytest.raises(ValueError, match="escapes"):
        ContainerManager._reject_container_escape("/etc/passwd")

    workspace = tmp_path / "proj"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    link = workspace / "link"
    link.symlink_to(outside)
    destination = local_init_destination(str(workspace), "notes.txt")
    assert destination == workspace.resolve() / "notes.txt"
    with pytest.raises(InitFilePathError):
        local_init_destination(str(workspace), "/cairn/dispatch.yaml")
    with pytest.raises(InitFilePathError):
        local_init_destination(str(workspace), "link")

    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    with pytest.raises(ValueError, match="escapes"):
        backend.write_text_file(str(workspace), str(outside), "overwrite")
    backend.write_text_file(str(workspace), str(destination), "ok")
    assert destination.read_text(encoding="utf-8") == "ok"


def test_unauthenticated_project_create_cannot_select_local_or_seed_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr("cairn.server.app.ADMIN_TOKEN", "")
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        plain = client.post("/projects", json={"title": "t", "origin": "o", "goal": "g"})
        assert plain.status_code == 201
        rooted = client.post(
            "/projects",
            json={"title": "t", "origin": "o", "goal": "g", "project_root": "/etc"},
        )
        assert rooted.status_code == 403
        codebase = client.post(
            "/projects",
            json={
                "title": "t",
                "origin": '{"codebase": {"path": "/etc"}}',
                "goal": "g",
            },
        )
        assert codebase.status_code == 403
        local = client.post(
            "/projects",
            json={"title": "t", "origin": "o", "goal": "g", "backend": "local"},
        )
        assert local.status_code == 403
        seeded = client.post(
            "/projects",
            json={
                "title": "t",
                "origin": "o",
                "goal": "g",
                "init_files": [{"path": "notes.txt", "content": "hello"}],
            },
        )
        assert seeded.status_code == 403
        escaped = client.post(
            "/projects",
            json={
                "title": "t",
                "origin": "o",
                "goal": "g",
                "init_files": [{"path": "/cairn/dispatch.yaml", "content": "server: x\n"}],
            },
        )
        assert escaped.status_code == 422

        monkeypatch.setattr("cairn.server.app.ADMIN_TOKEN", "test-admin")
        headers = {"Authorization": "Bearer test-admin"}
        escaped = client.post(
            "/projects",
            headers=headers,
            json={
                "title": "t",
                "origin": "o",
                "goal": "g",
                "init_files": [{"path": "/etc/passwd", "content": "x"}],
            },
        )
        assert escaped.status_code == 422
        allowed = client.post(
            "/projects",
            headers=headers,
            json={"title": "t", "origin": "o", "goal": "g", "backend": "local"},
        )
        assert allowed.status_code == 201
