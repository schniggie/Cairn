from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.local_process import LocalProcess

LOG = logging.getLogger(__name__)


class LocalBackend:
    """Runs workers directly on the dispatcher host instead of in per-project containers.

    Each project gets an isolated working directory under ``workspace_root`` (defaulting
    to the directory the dispatcher was started in). Worker processes inherit the host
    environment so the pre-configured ``pi`` CLI and its credentials are used as-is;
    no model-provider API keys are injected. There are no containers to build or tear
    down, so the container-lifecycle methods are inert.
    """

    def __init__(self, config: LocalConfig):
        self._config = config
        root = config.workspace_root
        self._root = Path(root).expanduser() if root else Path.cwd()

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return str(self._project_dir(project_id))

    def ensure_running(self, project_id: str, *, project_root: str | None = None) -> str:
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        self._link_project_root(project_dir, project_root)
        LOG.debug("local project workdir ready project=%s dir=%s", project_id, project_dir)
        return str(project_dir)

    @staticmethod
    def _link_project_root(project_dir: Path, project_root: str | None) -> None:
        if not project_root:
            return
        target = Path(project_root).expanduser().resolve()
        if not target.is_dir():
            LOG.warning("project_root is not a directory: %s", target)
            return
        link = project_dir / "project"
        if link.is_symlink() and link.resolve() == target:
            return
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.exists():
            return
        link.symlink_to(target, target_is_directory=True)

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
        stdin: str | None = None,
    ) -> LocalProcess:
        merged_env = {**os.environ, **(env or {})}
        return LocalProcess(
            command,
            cwd=container_name,
            env=merged_env,
            timeout_seconds=timeout_seconds,
            term_grace_seconds=kill_after_seconds,
            stdin=stdin,
        )

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        target = self._local_target(path)
        target.write_text(content, encoding="utf-8")

    def write_binary_file(self, container_name: str, path: str, data: bytes) -> None:
        self._local_target(path).write_bytes(data)

    def _local_target(self, path: str) -> Path:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"local file path must be absolute: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self._config.completed_action == "remove" and self._project_dir(project_id).exists()

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def cleanup_completed(self, project_id: str) -> bool:
        if self._config.completed_action == "remove":
            project_dir = self._project_dir(project_id)
            LOG.info("removing completed project workdir project=%s dir=%s", project_id, project_dir)
            shutil.rmtree(project_dir, ignore_errors=True)
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        return True

    def managed_container_names(self) -> list[str]:
        return []

    def _project_dir(self, project_id: str) -> Path:
        return self._root / project_id.replace("/", "-")
