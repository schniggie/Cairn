from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from cairn.server.services import compute_agent_context_hash, utcnow
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle, WorkEnvironment


@dataclass(frozen=True, slots=True)
class AgentContextMaterialization:
    enabled: bool
    status: str
    kind: str = "agents_md"
    content_hash: str | None = None
    materialized_path: str | None = None
    manifest_path: str | None = None
    source_template_id: str | None = None
    source_template_hash: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "materialized_path": self.materialized_path,
            "manifest_path": self.manifest_path,
            "source_template_id": self.source_template_id,
            "source_template_hash": self.source_template_hash,
            "status": self.status,
        }


class AgentContextMaterializationError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.metadata = metadata or {}


def materialize_agent_context(
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    context: dict[str, Any] | None,
) -> AgentContextMaterialization:
    target_path = str(PurePosixPath(handle.workspace) / "AGENTS.md")
    manifest_path = str(PurePosixPath(handle.workspace) / ".cairn" / "agent-context" / "agents_md.manifest.json")
    if context is None:
        return AgentContextMaterialization(enabled=False, status="disabled", materialized_path=target_path, manifest_path=manifest_path)
    kind = context.get("kind") or "agents_md"
    if kind != "agents_md":
        raise AgentContextMaterializationError(f"Unsupported agent context kind: {kind}")
    enabled = bool(context.get("enabled"))
    content = context.get("content") or ""
    content_hash = compute_agent_context_hash(content)
    metadata = {
        "enabled": enabled,
        "kind": kind,
        "content_hash": content_hash,
        "materialized_path": target_path,
        "manifest_path": manifest_path,
        "source_template_id": context.get("source_template_id"),
        "source_template_hash": context.get("source_template_hash"),
    }
    if not enabled:
        _cleanup_if_managed(environment, handle, target_path, manifest_path)
        return AgentContextMaterialization(status="disabled", **metadata)
    if not content.strip():
        raise AgentContextMaterializationError("Enabled agent context has empty content", metadata={**metadata, "status": "error"})

    if environment.exists(handle, target_path):
        current_hash = compute_agent_context_hash(environment.read_text_file(handle, target_path))
        if current_hash != content_hash and not _manifest_proves_managed(environment, handle, manifest_path, current_hash):
            raise AgentContextMaterializationError(
                "Workspace AGENTS.md exists and is not Cairn-managed",
                metadata={**metadata, "status": "conflict"},
            )
    environment.write_text_file(handle, target_path, content)
    manifest = {
        "kind": kind,
        "target_path": target_path,
        "last_materialized_hash": content_hash,
        "source_template_id": context.get("source_template_id"),
        "updated_at": utcnow(),
    }
    environment.write_text_file(handle, manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    return AgentContextMaterialization(status="materialized", **metadata)


def _manifest_proves_managed(environment: WorkEnvironment, handle: EnvironmentHandle, manifest_path: str, current_hash: str) -> bool:
    if not environment.exists(handle, manifest_path):
        return False
    try:
        manifest = json.loads(environment.read_text_file(handle, manifest_path))
    except json.JSONDecodeError:
        return False
    return isinstance(manifest, dict) and manifest.get("last_materialized_hash") == current_hash


def _cleanup_if_managed(environment: WorkEnvironment, handle: EnvironmentHandle, target_path: str, manifest_path: str) -> None:
    if not environment.exists(handle, target_path) or not environment.exists(handle, manifest_path):
        return
    current_hash = compute_agent_context_hash(environment.read_text_file(handle, target_path))
    if _manifest_proves_managed(environment, handle, manifest_path, current_hash):
        environment.delete_file(handle, target_path)
        environment.delete_file(handle, manifest_path)
