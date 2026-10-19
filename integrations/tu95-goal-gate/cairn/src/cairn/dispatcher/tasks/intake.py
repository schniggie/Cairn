from __future__ import annotations

import json
import logging

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_intake_payload
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    cancel_reason,
    did_timeout,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import IntakeClaimResponse


LOG = logging.getLogger(__name__)


def run_intake_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    claim: IntakeClaimResponse,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    lease = HeartbeatLease.for_intake(
        client,
        claim.project_id,
        worker.name,
        claim.claim_id,
        claim.revision,
        config.runtime.interval,
    )
    lease.start()
    try:
        # startup runtime 没有项目 id，不会创建 workspace/<project> 或注入项目 Docker shim。
        runtime_name = container_manager.create_startup_container()
        if task_healthcheck_enabled(config):
            healthcheck = run_healthcheck(
                container_manager,
                runtime_name,
                worker,
                driver.build_healthcheck(worker),
                timeout_seconds=config.runtime.healthcheck_timeout,
                lease=lease,
                cancellation=cancellation,
            )
            if cancel_reason(healthcheck.result, cancellation) is not None:
                return "cancelled"
            if lease.failure is not None:
                return "failed"
            if healthcheck.result.returncode != 0:
                _release(client, claim, worker, "intake worker healthcheck failed")
                return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "intake.md"),
            {
                "raw_origin": json.dumps(claim.raw_origin, ensure_ascii=False),
                "raw_goal": json.dumps(claim.raw_goal, ensure_ascii=False),
                "raw_hints": json.dumps(
                    [item.model_dump(mode="json") for item in claim.raw_hints],
                    ensure_ascii=False,
                    indent=2,
                ),
                "transcript": json.dumps(
                    [item.model_dump(mode="json") for item in claim.transcript],
                    ensure_ascii=False,
                    indent=2,
                ),
                "revision": str(claim.revision),
            },
        )
        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        result = run_worker_process(
            container_manager,
            runtime_name,
            worker,
            execute.argv,
            phase="intake",
            timeout_seconds=config.tasks.intake.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        if cancel_reason(result, cancellation) is not None:
            return "cancelled"
        if lease.failure is not None:
            return "failed"
        if did_timeout(result):
            _release(client, claim, worker, "intake model timeout")
            return "failed"
        if result.returncode != 0:
            _release(client, claim, worker, f"intake worker exited with code {result.returncode}")
            return "failed"

        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            kind, data = validate_intake_payload(parse_json_output(model_output))
        except Exception:
            LOG.warning(
                "intake output validation failed project=%s worker=%s revision=%s",
                claim.project_id,
                worker.name,
                claim.revision,
            )
            _release(client, claim, worker, "intake output validation failed")
            return "failed"
        if kind == "rejected" or data is None:
            _release(client, claim, worker, "intake model rejected review")
            return "rejected"

        response = client.decide_intake(
            claim.project_id,
            worker.name,
            claim.claim_id,
            claim.revision,
            kind=kind,
            data=data,
        )
        if not response.ok:
            LOG.warning(
                "intake decision failed project=%s worker=%s revision=%s status=%s",
                claim.project_id,
                worker.name,
                claim.revision,
                response.status_code,
            )
            return "failed"
        status = _project_status(response.data)
        if kind == "ready" and status in {"active", "ready_review"}:
            if status == "ready_review":
                LOG.info(
                    "intake ready_review project=%s revision=%s",
                    claim.project_id,
                    claim.revision,
                )
            else:
                LOG.info("intake activated project=%s mode=gate", claim.project_id)
            return "success"
        if kind == "questions" and status in {"waiting_input", "intake_failed"}:
            LOG.info(
                "intake decision stored project=%s status=%s question_count=%s revision=%s",
                claim.project_id,
                status,
                len(data),
                claim.revision,
            )
            return "success"
        # Server 会把语义失败持久化后返回 ProjectDetail；下一 tick 按新状态处理。
        return "failed"
    except Exception:
        LOG.exception(
            "intake task crashed project=%s worker=%s revision=%s",
            claim.project_id,
            worker.name,
            claim.revision,
        )
        if lease.failure is None and not cancellation.is_cancelled:
            _release(client, claim, worker, "intake dispatcher task failed")
        return "failed"
    finally:
        lease.stop()


def _release(
    client: CairnClient,
    claim: IntakeClaimResponse,
    worker: WorkerConfig,
    error: str,
) -> None:
    response = client.release_intake(
        claim.project_id,
        worker.name,
        claim.claim_id,
        claim.revision,
        error,
    )
    if not response.ok and response.status_code not in (403, 404, 409):
        LOG.warning(
            "intake release failed project=%s worker=%s revision=%s status=%s",
            claim.project_id,
            worker.name,
            claim.revision,
            response.status_code,
        )


def _project_status(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    status = project.get("status")
    return status if isinstance(status, str) else None
