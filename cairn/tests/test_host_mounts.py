"""Host mounts require an admin token and an approved source root."""

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.runtime.containers import host_bind_volumes
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.config import LocalConfig
from cairn.server import db
from cairn.server.app import app
from cairn.server.research_sandbox import HostMountError, resolve_approved_host_mount


def test_host_mount_rejects_sensitive_symlink_and_unset_root(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_PROJECT_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("CAIRN_RESEARCH_SOURCE_ROOT", raising=False)
    allowed = tmp_path / "src"
    allowed.mkdir()
    with pytest.raises(HostMountError, match="not configured"):
        resolve_approved_host_mount(str(allowed))

    monkeypatch.setenv("CAIRN_PROJECT_SOURCE_ROOT", "/")
    with pytest.raises(HostMountError, match="sensitive"):
        resolve_approved_host_mount(str(allowed))

    monkeypatch.setenv("CAIRN_PROJECT_SOURCE_ROOT", str(tmp_path))
    with pytest.raises(HostMountError, match="sensitive"):
        resolve_approved_host_mount("/")
    outside = tmp_path.parent / "outside-mount"
    outside.mkdir(exist_ok=True)
    with pytest.raises(HostMountError, match="outside"):
        resolve_approved_host_mount(str(outside))

    link = tmp_path / "link"
    link.symlink_to(allowed, target_is_directory=True)
    with pytest.raises(HostMountError, match="symlink"):
        resolve_approved_host_mount(str(link))
    escaped = tmp_path / "escape"
    escaped.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HostMountError, match="symlink"):
        resolve_approved_host_mount(str(escaped))

    approved = resolve_approved_host_mount(str(allowed))
    assert approved == allowed.resolve()
    volumes = host_bind_volumes(str(allowed), None)
    assert volumes[str(approved)]["bind"] == "/workspace/project"
    assert volumes[str(approved)]["mode"] == "ro"
    with pytest.raises(RuntimeError, match="project_root"):
        host_bind_volumes("/etc", None)

    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    project_dir = tmp_path / "work" / "proj"
    project_dir.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="outside"):
        backend._link_project_root(project_dir, str(outside))
    backend._link_project_root(project_dir, str(allowed))
    assert (project_dir / "project").resolve() == allowed.resolve()


def test_authenticated_project_can_record_an_approved_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr("cairn.server.app.ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("CAIRN_PROJECT_SOURCE_ROOT", str(tmp_path))
    db.configure(tmp_path / "cairn.db")
    source = tmp_path / "repo"
    source.mkdir()
    headers = {"Authorization": "Bearer test-admin"}
    with TestClient(app) as client:
        created = client.post(
            "/projects",
            headers=headers,
            json={"title": "t", "origin": "o", "goal": "g", "project_root": str(source)},
        )
        assert created.status_code == 201
        assert created.json()["project"]["project_root"] == str(source)
