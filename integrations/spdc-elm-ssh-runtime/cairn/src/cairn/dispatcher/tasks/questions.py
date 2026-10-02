from __future__ import annotations

import json
import logging
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import WorkEnvironment
from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import finish_execution_terminal, prepare_agent_context_for_execution, run_worker_process, _worker_secrets
from cairn.dispatcher.workers.registry import get_driver
from cairn.shared.worker_events import message_event, status_event
from cairn.shared.api_models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_question_task(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    project: ProjectDetail,
    worker: WorkerConfig,
    job: dict[str, Any],
    cancellation: TaskCancellation,
) -> str:
    if job.get("task_type") == "question" and job.get("id"):
        return _run_question_execution_task(config, client, environment, project, worker, job, cancellation)

    LOG.warning("legacy question job ignored; v3.2 uses execution question jobs")
    return "rejected"


def _run_question_execution_task(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    project: ProjectDetail,
    worker: WorkerConfig,
    execution: dict[str, Any],
    cancellation: TaskCancellation,
) -> str:
    dispatcher_id = "dispatcher"
    execution_id = execution["id"]
    sink_token = execution.get("sink_token")
    session_action = execution.get("session_action") or "fresh_context"
    mode = _mode_from_session_action(session_action)
    source_session = execution.get("remote_session_in_id")
    prompt = _prompt_from_execution(execution)
    driver = get_driver(worker.type)
    sink = ExecutionEventSink(
        client,
        execution_id,
        sink_token=sink_token,
        secrets=_worker_secrets(worker),
        event_projector=driver.stream_event_projector(execution_id),
        live_flush=False,
    )
    lease = HeartbeatLease.for_execution(client, execution_id, worker.name, config.runtime.interval, sink_token=sink_token)
    lease.start()
    running_patch = {"dispatcher_id": dispatcher_id, "status": "running"}
    if sink_token is not None:
        running_patch["sink_token"] = sink_token
    client.patch_execution(execution_id, running_patch)
    try:
        handle = environment.prepare_project(project.project.id)
        runtime_context = prepare_agent_context_for_execution(
            client,
            environment,
            handle,
            project_id=project.project.id,
            execution_id=execution_id,
            sink_token=sink_token,
        )
        driver_result = driver.build_question(
            worker,
            mode=mode,
            prompt=prompt,
            source_session=source_session,
            runtime_context=runtime_context,
        )
        run = run_worker_process(
            environment,
            handle,
            worker,
            driver_result.argv,
            phase=f"question_{mode}",
            timeout_seconds=300,
            project_id=project.project.id,
            task_type="question",
            intent_id=None,
            lease=lease,
            cancellation=cancellation,
            event_sink=sink,
            close_event_sink_on_finish=False,
            extra_metadata={
                "branch_id": execution.get("branch_id"),
                "session_action": session_action,
                "source_remote_session_id": source_session,
                "session_effect": "continued" if mode == "resume" else ("forked" if mode == "fork" else "fresh"),
            },
        )
    except Exception as exc:
        finish_execution_terminal(
            client,
            execution_id,
            sink_token=sink_token,
            events=[
                status_event(
                    "failed",
                    event_key=f"{execution_id}:status:worker-process-failed",
                    error_code="worker_process_failed",
                    error_detail=str(exc),
                ).to_api_payload()
            ],
            patch={
                "dispatcher_id": dispatcher_id,
                "status": "failed",
                "error_code": "worker_process_failed",
                "error_detail": str(exc),
            },
        )
        return "error"
    finally:
        lease.stop()

    sink.flush_projector_events()
    session = driver.extract_session_provenance(driver_result.session, run.stdout, run.stderr)
    answer = driver.extract_response_text(run.stdout, run.stderr).strip() or run.stderr.strip() or run.stdout.strip()
    if answer and not sink.has_projected_assistant_message:
        sink.write_event(
            message_event(
                "assistant",
                answer,
                event_key=f"{execution_id}:assistant:final",
            )
        )
    terminal_status = "cancelled" if run.cancelled else ("succeeded" if run.returncode == 0 else "failed")
    patch_fields = {
        "remote_session_out_kind": session.kind,
        "remote_session_out_id": session.id,
        "remote_session_out_status": session.status,
    }
    if run.run_log_ref is not None:
        patch_fields["metadata"] = {"raw_stream": run.run_log_ref}
    if lease.failure is not None:
        sink.write_event(
            message_event(
                "system",
                f"dispatcher diagnostic: heartbeat_cancelled status={lease.failure.status_code} detail={lease.failure.text}",
                event_key=f"{execution_id}:diagnostic:heartbeat-cancelled",
            )
        )
    closed = sink.close(
        terminal_status=terminal_status,
        returncode=run.returncode,
        error_code="timeout" if run.timed_out else None,
        error_detail=run.cancel_reason,
        patch_fields=patch_fields,
    )
    if not closed:
        return "error"
    if run.returncode != 0:
        return "error"
    return "success"


def _mode_from_session_action(session_action: str) -> str:
    if session_action == "fork_initial":
        return "fork"
    if session_action in {"resume_continue", "branch_continue"}:
        return "resume"
    return "fresh_context"


def _prompt_from_execution(execution: dict[str, Any]) -> str:
    snapshot = execution.get("input_snapshot") or {}
    return "\n".join(
        [
            "You are answering a short interactive Cairn follow-up question.",
            "Do not modify Cairn unless the user's question explicitly asks for writes.",
            "",
            "Execution JSON:",
            json.dumps(execution, ensure_ascii=False, sort_keys=True, indent=2),
            "",
            "User question:",
            snapshot.get("message") or "",
        ]
    )
