from __future__ import annotations

import posixpath
from typing import Any, Collection
import uuid

from cairn.dispatcher.config import DockerEnvironmentConfig, WorkerType
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle


class DockerEnvironment:
    backend = "docker"

    def __init__(self, config: DockerEnvironmentConfig):
        self.id = config.id
        self.label = config.label
        self._manager = ContainerManager(config.container)
        self._startup_handles: set[str] = set()

    def close(self) -> None:
        self._manager.close()

    def prepare_project(self, project_id: str) -> EnvironmentHandle:
        name = self._manager.ensure_running(project_id)
        workspace = self._workspace_for(project_id)
        self._manager.exec_mkdir(name, workspace)
        return EnvironmentHandle(project_id=project_id, target_name=name, workspace=workspace)

    def prepare_startup(self) -> EnvironmentHandle:
        name = self._manager.create_startup_container()
        self._startup_handles.add(name)
        workspace = f"/home/kali/workspace/.cairn/startup/{uuid.uuid4().hex}"
        self._manager.exec_mkdir(name, workspace)
        return EnvironmentHandle(project_id="startup", target_name=name, workspace=workspace)

    def cleanup_startup(self, handle: EnvironmentHandle) -> None:
        self._startup_handles.discard(handle.target_name)
        self._manager.remove_container(handle.target_name, force=True)

    def write_text_file(self, handle: EnvironmentHandle, path: str, content: str) -> None:
        if not self.is_path_in_workspace(handle, path):
            raise RuntimeError(f"refusing to write outside docker workspace: {path}")
        self._manager.write_text_file(handle.target_name, path, content)
        self._manager.exec_chmod_tree(handle.target_name, handle.workspace)

    def read_text_file(self, handle: EnvironmentHandle, path: str) -> str:
        if not self.is_path_in_workspace(handle, path):
            raise RuntimeError(f"refusing to read outside docker workspace: {path}")
        return self._manager.read_text_file(handle.target_name, path)

    def delete_file(self, handle: EnvironmentHandle, path: str) -> None:
        if not self.is_path_in_workspace(handle, path):
            raise RuntimeError(f"refusing to delete outside docker workspace: {path}")
        self._manager.delete_file(handle.target_name, path)

    def exists(self, handle: EnvironmentHandle, path: str) -> bool:
        if not self.is_path_in_workspace(handle, path):
            return False
        return self._manager.file_exists(handle.target_name, path)

    def is_path_in_workspace(self, handle: EnvironmentHandle, path: str) -> bool:
        workspace = posixpath.normpath(handle.workspace)
        target = posixpath.normpath(path)
        return target == workspace or target.startswith(workspace.rstrip("/") + "/")

    def graph_snapshot_path(self, handle: EnvironmentHandle, phase: str) -> str:
        return f"{handle.workspace}/.cairn/prompts/{phase}"

    def build_process(
        self,
        handle: EnvironmentHandle,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
        run_logger: Any | None = None,
    ):
        env = {**env, "CAIRN_WORKSPACE": handle.workspace}
        return self._manager.build_exec_process(
            handle.target_name,
            env,
            command,
            timeout_seconds=timeout_seconds,
            kill_after_seconds=kill_after_seconds,
            run_logger=run_logger,
        )

    def container_name(self, project_id: str) -> str:
        return self._manager.container_name(project_id)

    def _workspace_for(self, project_id: str) -> str:
        clean = project_id.replace("/", "-").replace("..", "-")
        return f"/home/kali/workspace/.cairn/projects/{clean}"

    def cleanup_key(self, project_id: str) -> str:
        return self.container_name(project_id)

    def managed_container_names(self) -> list[str]:
        return self._manager.managed_container_names()

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self._manager.needs_completed_cleanup(project_id)

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return self._manager.needs_stopped_cleanup(project_id)

    def cleanup_completed(self, project_id: str) -> bool:
        return self._manager.cleanup_completed(project_id)

    def cleanup_stopped(self, project_id: str) -> bool:
        return self._manager.cleanup_stopped(project_id)

    def cleanup_orphan(self, name: str) -> bool:
        return self._manager.cleanup_orphan(name)

    def needs_orphan_cleanup(self, name: str) -> bool:
        return self._manager.needs_orphan_cleanup(name)

    def run_healthcheck(self, worker_types: Collection[WorkerType] | None = None) -> dict[str, Any]:
        return {
            "environment_id": self.id,
            "backend": self.backend,
            "status": "skipped",
            "checks": [
                {
                    "name": "docker",
                    "status": "skipped",
                    "duration_ms": 0,
                    "command": "startup worker healthcheck uses Docker exec",
                    "stdout": "",
                    "stderr": "",
                }
            ],
        }
