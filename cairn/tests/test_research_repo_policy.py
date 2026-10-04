"""Repository mounts stay inside an operator-configured source root."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.research_sandbox import RepoPathError, build_sandbox, resolve_approved_repo
from cairn.server.routers.research import router


def test_rejects_root_home_and_unconfigured_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_RESEARCH_SOURCE_ROOT", raising=False)
    repo = tmp_path / "app"
    repo.mkdir()
    with pytest.raises(RepoPathError, match="CAIRN_RESEARCH_SOURCE_ROOT"):
        resolve_approved_repo(str(repo))
    with pytest.raises(RepoPathError, match="敏感"):
        resolve_approved_repo("/")
    if Path("/etc").is_dir():
        with pytest.raises(RepoPathError, match="敏感"):
            resolve_approved_repo("/etc")
    home = Path.home()
    if home.is_dir():
        with pytest.raises(RepoPathError, match="敏感"):
            resolve_approved_repo(str(home))


def test_repo_must_stay_inside_source_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CAIRN_RESEARCH_SOURCE_ROOT", str(allowed))
    assert resolve_approved_repo(str(allowed)) == allowed.resolve()
    nested = allowed / "proj"
    nested.mkdir()
    assert resolve_approved_repo(str(nested)) == nested.resolve()
    with pytest.raises(RepoPathError, match="已批准"):
        resolve_approved_repo(str(outside))
    monkeypatch.setenv("CAIRN_RESEARCH_SOURCE_ROOT", "/")
    with pytest.raises(RepoPathError, match="敏感"):
        resolve_approved_repo(str(nested))


def test_sandbox_refuses_sensitive_repo_before_bind(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="敏感"):
        build_sandbox(workspace=workspace, repo="/", argv=["-c", "true"], claude_bin="sh")


def test_api_does_not_treat_authorization_flag_as_approval(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setenv("CAIRN_RESEARCH_SOURCE_ROOT", str(allowed))
    db.configure(tmp_path / "research.db")
    app = FastAPI()
    app.include_router(router)
    body = {
        "title": "scope",
        "objective": "review",
        "repo": str(outside),
        "authorization_confirmed": True,
    }
    with TestClient(app) as client:
        response = client.post("/api/research/sessions", json=body)
        assert response.status_code == 422
        assert "已批准" in response.text
        sensitive = client.post("/api/research/sessions", json={**body, "repo": "/etc"})
        assert sensitive.status_code == 422
        assert "敏感" in sensitive.text
        ok = client.post("/api/research/sessions", json={**body, "repo": str(allowed)})
        assert ok.status_code == 201, ok.text
