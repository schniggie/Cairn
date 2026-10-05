from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

from cairn.server.models import ContinueEvidence
from cairn.workspace import workspace_path


def attest_continue_evidence(project_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """把模型给出的相对路径转换为由 Dispatcher 实际验证的文件证据。"""
    raw_artifacts = checkpoint.get("artifacts") or []
    raw_milestones = checkpoint.get("milestones") or []
    paths = list(raw_artifacts)
    paths.extend(item["artifact"] for item in raw_milestones)

    artifacts = [_artifact_ref(project_id, path) for path in sorted(set(paths))]
    evidence = ContinueEvidence.model_validate(
        {
            "artifacts": artifacts,
            "cursors": dict(sorted((checkpoint.get("cursors") or {}).items())),
            "milestones": sorted(raw_milestones, key=lambda item: item["id"]),
        }
    )
    return evidence.model_dump(mode="json")


def retry_after_for_attempt(attempt_count: int, max_session_attempts: int, retry_delay: int) -> int | None:
    return retry_delay if attempt_count < max_session_attempts else None


def _artifact_ref(project_id: str, raw_path: str) -> dict[str, Any]:
    relative = _normalized_relative_path(raw_path)
    root = workspace_path(project_id).resolve()
    target = (root / Path(*relative.parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes project workspace: {raw_path}") from exc
    if not target.is_file():
        raise ValueError(f"artifact does not exist or is not a file: {raw_path}")

    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"path": relative.as_posix(), "sha256": digest.hexdigest(), "size": size}


def _normalized_relative_path(raw_path: str) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("artifact path must not be empty")
    text = raw_path.strip()
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or text in {".", ".."}
        or "\\" in text
        or path.as_posix() != text
        or text.startswith("./")
    ):
        raise ValueError(f"artifact path must be normalized and relative: {raw_path}")
    return path
