from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cairn import skills_store
from cairn.dispatcher.prompting import format_project_knowledge, format_skills

from cairn.dispatcher.config import DispatchConfig, SafetyConfig, WorkerConfig, resolve_safety_token
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.base import DriverResult
from cairn.server.models import AuditEvent, AuditEventCreate

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/cairn-prompts"
TRAJECTORY_DIR = Path(os.environ.get("CAIRN_TRAJECTORY_DIR", "trajectories"))
LOG = logging.getLogger(__name__)
SAFETY_FALLBACK_PREFIX = "CAIRN_SAFETY_FALLBACK "


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


@dataclass(frozen=True, slots=True)
class SafetyRunContext:
    run_id: str
    project_id: str
    intent_id: str | None
    worker: str
    phase: str


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def parse_safety_fallbacks(stderr: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in stderr.splitlines():
        if not line.startswith(SAFETY_FALLBACK_PREFIX):
            continue
        try:
            record = json.loads(line[len(SAFETY_FALLBACK_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            continue
        failed_event = record.get("failed_event")
        transport_error = record.get("transport_error")
        if not isinstance(failed_event, dict) or not isinstance(transport_error, str) or not transport_error:
            continue
        original_event_id = failed_event.get("event_id")
        if not isinstance(original_event_id, str) or not original_event_id:
            continue
        candidate = {
            "schema_version": 1,
            "event_id": f"backfill:{original_event_id}",
            "action_id": failed_event.get("action_id"),
            "run_id": failed_event.get("run_id"),
            "project_id": failed_event.get("project_id"),
            "intent_id": failed_event.get("intent_id"),
            "worker": failed_event.get("worker"),
            "phase": failed_event.get("phase"),
            "event_type": "AUDIT_BACKFILL",
            "tool_name": failed_event.get("tool_name"),
            "decision": failed_event.get("decision"),
            "rule_id": failed_event.get("rule_id"),
            "reason": failed_event.get("reason"),
            "payload": {
                "original_event_id": original_event_id,
                "failed_event": failed_event,
                "transport_error": transport_error,
            },
        }
        try:
            validated = AuditEventCreate.model_validate(candidate)
        except ValueError:
            continue
        events.append(validated.model_dump(mode="json"))
    return events


def backfill_safety_fallbacks(
    client: CairnClient,
    stderr: str,
    safety: SafetyConfig | None,
) -> int:
    events = parse_safety_fallbacks(stderr)
    if not events or safety is None or not safety.enabled:
        return 0
    try:
        token = resolve_safety_token(safety)
    except ValueError as exc:
        LOG.error("cannot backfill safety events because the shared token is unavailable: %s", exc)
        return 0
    written = 0
    for event in events:
        response = client.backfill_audit_event(event, token)
        if response.ok:
            written += 1
            continue
        LOG.warning(
            "safety audit backfill failed event=%s project=%s status=%s body=%s",
            event.get("event_id"),
            event.get("project_id"),
            response.status_code,
            response.text,
        )
    return written


def latest_blocked_action(
    client: CairnClient,
    project_id: str,
    run_id: str,
) -> AuditEvent | None:
    events: list[AuditEvent] = []
    for event_type in ("ACTION_DECISION", "AUDIT_BACKFILL"):
        events.extend(
            client.list_audit_events(
                project_id,
                run_id=run_id,
                event_type=event_type,
                limit=100,
            )
        )
    limited = [event for event in events if event.decision in ("block", "resource_pause")]
    if not limited:
        return None
    return max(limited, key=lambda event: (event.created_at, event.event_id))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def project_origin_description(project) -> str | None:
    for fact in getattr(project, "facts", []) or []:
        if getattr(fact, "id", None) == "origin":
            return fact.description
    return None


def origin_codebase_host_path(origin_description: str | None) -> str | None:
    if not origin_description:
        return None
    try:
        payload = json.loads(origin_description)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    codebase = payload.get("codebase") or {}
    path = codebase.get("path") if isinstance(codebase, dict) else None
    if isinstance(path, str) and path.strip():
        return path.strip()
    return None


def resolve_codebase_host_path(project, *, require_readable: bool = True) -> tuple[str | None, str | None]:
    host_path = origin_codebase_host_path(project_origin_description(project))
    if not host_path:
        return None, None
    if not require_readable:
        return host_path, None
    path = Path(host_path)
    if not path.exists():
        return None, f"codebase path does not exist: {host_path}"
    if not os.access(path, os.R_OK):
        return None, f"codebase path is not readable: {host_path}"
    return str(path), None


def ensure_static_container(config: DispatchConfig, container_manager: object, project) -> tuple[str | None, str | None]:
    host_path, err = resolve_codebase_host_path(project, require_readable=True)
    if err:
        LOG.error("static container codebase bind failed project=%s error=%s", project.project.id, err)
        return None, err
    name = container_manager.ensure_running(  # type: ignore[attr-defined]
        project.project.id,
        profile="static",
        codebase_host_path=host_path,
        project_root=getattr(project.project, "project_root", None),
    )
    return name, None


def knowledge_prompt(container_manager: object, container_name: str, project_root: str | None) -> dict[str, str]:
    return {
        "skills": prepare_skills(container_manager, container_name),
        "project_knowledge": prepare_project_knowledge(project_root),
    }


def prepare_skills(runtime: object, workspace_key: str) -> str:
    """Copy enabled skills into the worker workspace and return prompt text."""
    metas = [meta for meta in skills_store.list_skills() if meta.enabled]
    if not metas:
        return ""
    for skill_dir in skills_store.enabled_skill_dirs():
        for path in skill_dir.rglob("*"):
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            relative = path.relative_to(skill_dir).as_posix()
            dest = _skill_dest(workspace_key, skill_dir.name, relative)
            try:
                runtime.write_text_file(workspace_key, dest, path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
            except UnicodeDecodeError:
                if hasattr(runtime, "write_binary_file"):
                    runtime.write_binary_file(workspace_key, dest, path.read_bytes())  # type: ignore[attr-defined]
            except Exception:
                LOG.debug("failed to install skill file %s", path, exc_info=True)
    return format_skills(metas)


def prepare_project_knowledge(project_root: str | None) -> str:
    if not project_root:
        return ""
    root = Path(project_root).expanduser()
    present = [name for name in ("src-repo", "docs-out", "graphify-out", "scan-out", "codegraph-out") if (root / name).is_dir()]
    return format_project_knowledge(str(root), present)


def _skill_dest(workspace_key: str, name: str, relative: str) -> str:
    if workspace_key.startswith("/"):
        return str(Path(workspace_key) / ".claude" / "skills" / name / relative)
    return f"/workspace/.claude/skills/{name}/{relative}"


def write_conclude_result_with_observations(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    observations: list[dict],
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    base_knowledge_patches: list[dict] | None = None,
) -> str:
    response = client.conclude_observations(
        project_id,
        intent_id,
        worker_name,
        observations,
        base_knowledge_patches=base_knowledge_patches,
    )
    if response.ok:
        LOG.info(
            "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s observations=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            phase_ms,
            total_ms,
            len(observations),
        )
        return "success"
    LOG.warning(
        "conclude observations failed project=%s intent=%s status=%s body=%s",
        project_id,
        intent_id,
        response.status_code,
        preview(response.text),
    )
    return "failed"


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    if config.runtime.execution == "local":
        return False
    return config.runtime.worker_healthcheck == "startup_and_task"


def save_session_log(
    container_manager: object,
    container_name: str,
    project_id: str,
    worker_name: str,
    session: str | None,
    *,
    phase: str,
) -> None:
    """Copy worker session logs out before container cleanup so trajectories survive."""
    if not session or not hasattr(container_manager, "build_exec_process"):
        return
    try:
        find_result = container_manager.build_exec_process(  # type: ignore[attr-defined]
            container_name,
            {},
            ["find", "/tmp", "-name", "*.jsonl"],
        )
        find_result.start()
        find_output = find_result.communicate(timeout=10)
        if find_output.returncode != 0 or not find_output.stdout.strip():
            return
        out_dir = TRAJECTORY_DIR / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        for session_path in find_output.stdout.strip().split("\n"):
            session_path = session_path.strip()
            if not session_path:
                continue
            cat_result = container_manager.build_exec_process(  # type: ignore[attr-defined]
                container_name, {}, ["cat", session_path]
            )
            cat_result.start()
            cat_output = cat_result.communicate(timeout=30)
            if cat_output.returncode != 0 or not cat_output.stdout:
                continue
            dest = out_dir / f"{phase}_{worker_name}_{Path(session_path).name}"
            dest.write_text(cat_output.stdout, encoding="utf-8")
            LOG.info("saved session log project=%s worker=%s phase=%s path=%s", project_id, worker_name, phase, dest)
    except Exception:
        LOG.debug("failed to save session log project=%s worker=%s", project_id, worker_name, exc_info=True)


def persist_http_records(
    client: CairnClient,
    project_id: str,
    intent_id: str | None,
    worker_name: str,
    records: list[dict],
) -> None:
    for record in records:
        payload = {**record, "intent_id": intent_id, "worker": worker_name}
        response = client.create_http_record(project_id, payload)
        if response.ok:
            continue
        LOG.warning(
            "http evidence write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            preview(response.text),
        )


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def apply_worker_subprocess_guards(worker: WorkerConfig, env: dict[str, str]) -> dict[str, str]:
    """Keep provider credentials out of Claude Code tool subprocesses.

    Claude Code 2.1.98 strips Anthropic and cloud-provider credentials from Bash,
    hook, and MCP stdio children when this variable is ``1``. The parent process
    still needs the token to call the model API. This does not close worker egress.
    """
    guarded = dict(env)
    if worker.type == "claudecode":
        guarded["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    return guarded


def run_worker_process(
    container_manager: ExecutionBackend,
    container_name: str,
    worker: WorkerConfig,
    command: DriverResult,
    *,
    phase: str,
    timeout_seconds: int,
    safety: SafetyConfig | None = None,
    safety_context: SafetyRunContext | None = None,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    stdin: str | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    if stdin is None:
        stdin = command.stdin
    for asset in command.assets:
        container_manager.write_text_file(container_name, asset.path, asset.content)

    exec_env = apply_worker_subprocess_guards(worker, dict(worker.env))
    project_id = safety_context.project_id if safety_context is not None else None
    if project_id and hasattr(container_manager, "project_env"):
        exec_env.update(container_manager.project_env(project_id))
    if safety is not None and safety.enabled and safety_context is not None:
        if safety_context.worker != worker.name:
            raise ValueError("safety context worker does not match worker config")
        if safety_context.phase != phase:
            raise ValueError("safety context phase does not match execution phase")
        exec_env.update(
            {
                "CAIRN_SAFETY_ENDPOINT": safety.endpoint.rstrip("/"),
                "CAIRN_SAFETY_TOKEN": resolve_safety_token(safety),
                "CAIRN_PROJECT_ID": safety_context.project_id,
                "CAIRN_INTENT_ID": safety_context.intent_id or "",
                "CAIRN_RUN_ID": safety_context.run_id,
                "CAIRN_WORKER": safety_context.worker,
                "CAIRN_PHASE": safety_context.phase,
                "CAIRN_SAFETY_TIMEOUT_MS": str(safety.request_timeout_ms),
                "CAIRN_SAFETY_MAX_PAYLOAD_BYTES": str(safety.max_payload_bytes),
                "CAIRN_SAFETY_MAX_BULK_CONCURRENCY": str(
                    safety.resource_budget.max_bulk_concurrency
                ),
                "CAIRN_SAFETY_MAX_UNATTENDED_BULK_SECONDS": str(
                    safety.resource_budget.max_unattended_bulk_seconds
                ),
                "CAIRN_SAFETY_AUTH_CONCURRENCY": str(safety.resource_budget.auth_concurrency),
                "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE": str(
                    safety.resource_budget.auth_attempts_per_minute
                ),
                "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_BATCH": str(
                    safety.resource_budget.auth_attempts_per_batch
                ),
            }
        )

    process = container_manager.build_exec_process(
        container_name,
        exec_env,
        command.argv,
        timeout_seconds=timeout_seconds,
        stdin=stdin,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def project_allows_conclude_fallback(client: CairnClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: CairnClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    ).status


def write_conclude_result_with_fact_id(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(project_id, intent_id, worker_name, description)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: CairnClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
