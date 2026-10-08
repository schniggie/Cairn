"""Keep API-supplied init files inside one project workspace.

Container writes use the in-container ``/workspace`` tree. Local writes use the
project directory on the host. Absolute paths outside that tree, ``..``, and
symlinks that resolve outside the tree are rejected.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

CONTAINER_WORKSPACE = "/workspace"


class InitFilePathError(ValueError):
    """The init-file path is not confined to the project workspace."""


def relative_init_path(requested: str) -> PurePosixPath:
    """Return the workspace-relative path for an API init file.

    ``/workspace/notes.txt`` and ``notes.txt`` both mean ``notes.txt``. Any other
    absolute path is outside the workspace.
    """
    text = requested.strip().replace("\\", "/")
    if not text or "\x00" in text:
        raise InitFilePathError("init file path is empty")
    path = PurePosixPath(text)
    if any(part == ".." for part in path.parts):
        raise InitFilePathError("init file path cannot contain ..")
    if path.is_absolute():
        try:
            relative = path.relative_to(CONTAINER_WORKSPACE)
        except ValueError as exc:
            raise InitFilePathError("init file path must stay inside the project workspace") from exc
    else:
        relative = path
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise InitFilePathError("init file path must name a file inside the project workspace")
    return relative


def container_init_destination(requested: str) -> str:
    return str(PurePosixPath(CONTAINER_WORKSPACE) / relative_init_path(requested))


def local_init_destination(workspace: str, requested: str) -> Path:
    """Resolve ``requested`` under ``workspace`` without following a symlink out."""
    root_path = Path(workspace)
    if root_path.is_symlink():
        raise InitFilePathError("project workspace must not be a symlink")
    root = root_path.resolve()
    current = root
    for part in relative_init_path(requested).parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve()
            if not target.is_relative_to(root):
                raise InitFilePathError("init file path escapes through a symlink")
            current = target
    if current.exists():
        resolved = current.resolve()
    else:
        parent = current.parent.resolve()
        if parent != root and not parent.is_relative_to(root):
            raise InitFilePathError("init file path escapes the project workspace")
        resolved = parent / current.name
    if resolved != root and not resolved.is_relative_to(root):
        raise InitFilePathError("init file path escapes the project workspace")
    if resolved == root:
        raise InitFilePathError("init file path must name a file inside the project workspace")
    return resolved
