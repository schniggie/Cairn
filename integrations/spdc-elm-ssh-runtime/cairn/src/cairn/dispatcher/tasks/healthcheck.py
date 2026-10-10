from __future__ import annotations

import logging
import shlex

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.models import TaskOutcome
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.redaction import redact_text
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import WorkEnvironment
from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.tasks.common import run_healthcheck, worker_health_from_healthcheck
from cairn.dispatcher.workers.registry import get_driver
from cairn.shared.worker_events import WorkerEvent

LOG = logging.getLogger(__name__)


def run_healthcheck_task(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
    execution_id: str,
    sink_token: str | None = None,
) -> TaskOutcome:
    driver = get_driver(worker.type)
    secrets = _worker_secrets(worker)
    sink = ExecutionEventSink(client, execution_id, sink_token=sink_token, secrets=secrets)
    command = driver.build_healthcheck(worker)
    described_command = redact_text(shlex.join(command), secrets)
    status = "failed"
    returncode = 1
    health_payload: dict | None = None
    sink.write_status(
        "running",
        environment_id=environment.id,
        worker_name=worker.name,
        worker_type=worker.type,
    )
    running_patch = {
        "dispatcher_id": "dispatcher",
        "status": "running",
        "lease_seconds": max(60, config.runtime.healthcheck_timeout + 20),
    }
    if sink_token is not None:
        running_patch["sink_token"] = sink_token
    patch = client.patch_execution(execution_id, running_patch)
    if not patch.ok:
        LOG.warning("manual healthcheck running patch failed execution=%s status=%s body=%s", execution_id, patch.status_code, patch.text)
    handle = None
    try:
        handle = environment.prepare_startup()
        healthcheck = run_healthcheck(
            environment,
            handle,
            worker,
            command,
            timeout_seconds=config.runtime.healthcheck_timeout,
            cancellation=cancellation,
        )
        returncode = healthcheck.result.returncode
        if healthcheck.result.stdout:
            sink.write_stream("stdout", healthcheck.result.stdout)
        if healthcheck.result.stderr:
            sink.write_stream("stderr", healthcheck.result.stderr)
        ok = returncode == 0
        status = "succeeded" if ok else "failed"
        error_code = None if ok else "healthcheck_failed"
        error_detail = None
        if not ok and not healthcheck.result.stdout and not healthcheck.result.stderr:
            error_detail = f"healthcheck exited with returncode {returncode} without stdout/stderr"
            sink.write_stream("stderr", f"{error_detail}\n")
        health_payload = worker_health_from_healthcheck(
            environment,
            worker,
            healthcheck,
            status="ok" if ok else "unhealthy",
            source="manual_healthcheck",
            command=described_command,
        )
        stale_after = 300 if ok else 30
        health_payload["stale_after"] = _future_iso(stale_after)
        if not ok:
            health_payload["disabled_until"] = _future_iso(5)
        sink.write_event(
            WorkerEvent(
                event_type="metric",
                payload={
                    "duration_ms": healthcheck.duration_ms,
                    "returncode": returncode,
                    "command": described_command,
                },
                event_key=f"{execution_id}:metric:healthcheck",
            )
        )
        sink.close(terminal_status=status, returncode=returncode, error_code=error_code, error_detail=error_detail)
        return TaskOutcome("success" if ok else "unhealthy", worker_health=health_payload)
    except Exception as exc:
        LOG.exception("manual healthcheck crashed execution=%s worker=%s", execution_id, worker.name)
        sink.write_stream("stderr", str(exc))
        sink.close(terminal_status="failed", returncode=returncode, error_code="healthcheck_crashed", error_detail=str(exc))
        return TaskOutcome("unhealthy", worker_health=health_payload)
    finally:
        if handle is not None:
            environment.cleanup_startup(handle)


def _worker_secrets(worker: WorkerConfig) -> list[str]:
    return [
        value
        for key, value in worker.env.items()
        if value and ("KEY" in key or "TOKEN" in key or "SECRET" in key)
    ]


def _future_iso(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
