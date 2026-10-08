from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from cairn.dispatcher.config import AuthConfig, LocalConfig
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.server.research_sandbox import HostMountError, resolve_approved_host_mount

LOG = logging.getLogger(__name__)


def _dispatcher_owned(resolved: Path) -> bool:
    """Prompt snapshots and Pi assets are written by the dispatcher, not by init files."""
    from cairn.dispatcher.tasks import common as common_mod

    roots = [Path(common_mod.GRAPH_SNAPSHOT_ROOT), Path("/tmp/cairn-pi")]
    for root in roots:
        try:
            base = root.resolve()
        except OSError:
            continue
        if resolved == base or resolved.is_relative_to(base):
            return True
    return False


def _lexical_destination(target: Path) -> Path:
    parent = target.parent
    if parent.exists():
        return parent.resolve() / target.name
    return Path(os.path.abspath(target))


class LocalBackend:
    """Runs workers directly on the dispatcher host instead of in per-project containers.

    Each project gets an isolated working directory under ``workspace_root`` (defaulting
    to the directory the dispatcher was started in). Worker processes inherit the host
    environment so the pre-configured ``pi`` CLI and its credentials are used as-is;
    no model-provider API keys are injected. There are no containers to build or tear
    down, so the container-lifecycle methods are inert.
    """

    def __init__(self, config: LocalConfig, auth_config: AuthConfig | None = None):
        self._config = config
        self._auth_config = auth_config
        root = config.workspace_root
        self._root = Path(root).expanduser() if root else Path.cwd()

    def close(self) -> None:
        return None

    def project_env(self, project_id: str) -> dict[str, str]:
        env = {"CAIRN_PROJECT_ID": project_id}
        auth = getattr(self, "_auth_config", None)
        if auth is not None:
            env["CAIRN_AUTH_DIR"] = f"{auth.store_root.rstrip('/')}/{project_id}"
        return env

    def container_name(self, project_id: str) -> str:
        return str(self._project_dir(project_id))

    def ensure_running(
        self,
        project_id: str,
        *,
        project_root: str | None = None,
        profile: str | None = None,
        codebase_host_path: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        self._link_project_root(project_dir, project_root or codebase_host_path)
        if profile or extra_env:
            LOG.debug("local ensure_running project=%s profile=%s", project_id, profile)
        LOG.debug("local project workdir ready project=%s dir=%s", project_id, project_dir)
        return str(project_dir)

    @staticmethod
    def _link_project_root(project_dir: Path, project_root: str | None) -> None:
        if not project_root:
            return
        try:
            target = resolve_approved_host_mount(project_root)
        except HostMountError as exc:
            raise RuntimeError(str(exc)) from exc
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
        target = self._confined_target(container_name, path)
        target.write_text(content, encoding="utf-8")

    def write_binary_file(self, container_name: str, path: str, data: bytes) -> None:
        self._confined_target(container_name, path).write_bytes(data)

    def _confined_target(self, container_name: str, path: str) -> Path:
        """Write an absolute path that stays inside the project workspace.

        ``container_name`` is the project directory for local execution. Init files
        are resolved there before this call. Dispatcher files under that directory
        (skills) are allowed. Paths outside it are rejected, except the two
        dispatcher trees ``/tmp/cairn-prompts`` and ``/tmp/cairn-pi``.
        """
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"local file path must be absolute: {path}")
        workspace = Path(container_name)
        if workspace.is_absolute() and workspace.is_dir() and not workspace.is_symlink():
            root = workspace.resolve()
            resolved = target.resolve() if target.exists() else _lexical_destination(target)
            inside = resolved == root or resolved.is_relative_to(root)
            if not inside and not _dispatcher_owned(resolved):
                raise ValueError(f"local file path escapes the project workspace: {path}")
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
