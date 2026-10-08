from __future__ import annotations

from pathlib import Path

from cairn.dispatcher.config import AuthConfig, LocalConfig
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.local_backend import LocalBackend


def _auth_config() -> AuthConfig:
    return AuthConfig.model_validate(
        {
            "store_root": "/opt/cairn/auth",
            "worker_mount_root": "/run/cairn-auth",
            "targets": [],
        }
    )


def test_container_auth_volumes_are_read_only(tmp_path: Path) -> None:
    # Bypass Docker client construction: we only exercise the mount computation.
    manager = object.__new__(ContainerManager)
    manager._auth_config = _auth_config()

    volumes = manager._auth_volumes("proj_001")

    assert "/opt/cairn/auth/proj_001" in volumes
    assert volumes["/opt/cairn/auth/proj_001"]["bind"] == "/run/cairn-auth"
    assert volumes["/opt/cairn/auth/proj_001"]["mode"] == "ro"


def test_container_auth_volumes_empty_without_auth_config(tmp_path: Path) -> None:
    manager = object.__new__(ContainerManager)
    manager._auth_config = None
    assert manager._auth_volumes("proj_001") == {}


def test_container_project_env_points_at_worker_mount(tmp_path: Path) -> None:
    manager = object.__new__(ContainerManager)
    manager._auth_config = _auth_config()
    env = manager.project_env("proj_001")
    assert env["CAIRN_PROJECT_ID"] == "proj_001"
    assert env["CAIRN_AUTH_DIR"] == "/run/cairn-auth"


def test_container_project_env_without_auth(tmp_path: Path) -> None:
    manager = object.__new__(ContainerManager)
    manager._auth_config = None
    env = manager.project_env("proj_001")
    assert env["CAIRN_PROJECT_ID"] == "proj_001"
    assert "CAIRN_AUTH_DIR" not in env


def test_local_backend_project_env_points_at_host_store(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)), _auth_config())
    env = backend.project_env("proj_001")
    assert env["CAIRN_PROJECT_ID"] == "proj_001"
    assert env["CAIRN_AUTH_DIR"] == "/opt/cairn/auth/proj_001"


def test_local_backend_project_env_without_auth(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    env = backend.project_env("proj_001")
    assert env["CAIRN_PROJECT_ID"] == "proj_001"
    assert "CAIRN_AUTH_DIR" not in env
