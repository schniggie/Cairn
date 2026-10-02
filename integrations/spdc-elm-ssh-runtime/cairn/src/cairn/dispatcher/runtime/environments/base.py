from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Protocol

from cairn.dispatcher.config import WorkerType
from cairn.dispatcher.runtime.process import ProcessResult


@dataclass(frozen=True, slots=True)
class EnvironmentHandle:
    project_id: str
    target_name: str
    workspace: str


@dataclass(frozen=True, slots=True)
class EnvironmentState:
    environment_id: str
    label: str
    backend: str
    target_name: str
    workspace: str | None = None


class ManagedProcessLike(Protocol):
    def start(self) -> None: ...

    def communicate(self, timeout: float | None) -> ProcessResult: ...

    def kill(self) -> None: ...

    def cancel(self, reason: str) -> None: ...


class WorkEnvironment(Protocol):
    id: str
    label: str
    backend: str

    def close(self) -> None: ...

    def prepare_project(self, project_id: str) -> EnvironmentHandle: ...

    def prepare_startup(self) -> EnvironmentHandle: ...

    def cleanup_startup(self, handle: EnvironmentHandle) -> None: ...

    def write_text_file(self, handle: EnvironmentHandle, path: str, content: str) -> None: ...

    def read_text_file(self, handle: EnvironmentHandle, path: str) -> str: ...

    def delete_file(self, handle: EnvironmentHandle, path: str) -> None: ...

    def exists(self, handle: EnvironmentHandle, path: str) -> bool: ...

    def is_path_in_workspace(self, handle: EnvironmentHandle, path: str) -> bool: ...

    def graph_snapshot_path(self, handle: EnvironmentHandle, phase: str) -> str: ...

    def build_process(
        self,
        handle: EnvironmentHandle,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
        run_logger: Any | None = None,
    ) -> ManagedProcessLike: ...

    def needs_completed_cleanup(self, project_id: str) -> bool: ...

    def needs_stopped_cleanup(self, project_id: str) -> bool: ...

    def cleanup_completed(self, project_id: str) -> bool: ...

    def cleanup_stopped(self, project_id: str) -> bool: ...

    def cleanup_key(self, project_id: str) -> str: ...

    def run_healthcheck(self, worker_types: Collection[WorkerType] | None = None) -> dict[str, Any]: ...
