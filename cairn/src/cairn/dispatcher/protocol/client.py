from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import os
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.server.models import AuditEvent, Intent, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)
ADMIN_TOKEN = os.environ.get("CAIRN_ADMIN_TOKEN", "")


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(self, base_url: str, timeout: float = 10.0, server_token: str | None = None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._server_token = server_token
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def update_settings(self, timeout: int) -> Settings:
        response = self._session().put(
            self._url("/settings"),
            json={"intent_timeout": timeout, "reason_timeout": timeout},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def update_project_status(self, project_id: str, status: str) -> ApiResult:
        return self._request_json(
            "PUT",
            f"/projects/{project_id}/status",
            json={"status": status},
        )

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def list_audit_events(
        self,
        project_id: str,
        *,
        intent_id: str | None = None,
        run_id: str | None = None,
        event_type: str | None = None,
        decision: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        params = {
            "intent_id": intent_id,
            "run_id": run_id,
            "event_type": event_type,
            "decision": decision,
            "limit": limit,
        }
        response = self._session().get(
            self._url(f"/projects/{project_id}/audit"),
            params={key: value for key, value in params.items() if value is not None},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return TypeAdapter(list[AuditEvent]).validate_python(response.json()["items"])

    def backfill_audit_event(self, payload: dict[str, Any], token: str) -> ApiResult:
        return self._request_json(
            "POST",
            "/internal/safety/events",
            json=payload,
            headers={"X-Cairn-Safety-Token": token},
        )

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        creator: str,
        worker: str | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={"from": from_ids, "description": description, "creator": creator, "worker": worker},
        )

    def create_runtime_event(
        self,
        project_id: str,
        *,
        event_type: str,
        status: str,
        message: str,
        phase: str | None = None,
        worker: str | None = None,
        intent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/events",
            json={
                "event_type": event_type,
                "phase": phase,
                "status": status,
                "message": message,
                "worker": worker,
                "intent_id": intent_id,
                "payload": payload,
            },
        )

    def create_http_record(self, project_id: str, record: dict[str, Any]) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/http-records", json=record)

    def record_failure(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        stale_retry_threshold: int = 3,
        dead_retry_threshold: int = 10,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/fail",
            json={
                "worker": worker,
                "stale_retry_threshold": stale_retry_threshold,
                "dead_retry_threshold": dead_retry_threshold,
            },
        )

    def get_verify_control(self, project_id: str) -> dict:
        response = self._session().get(self._url(f"/projects/{project_id}/verify/control"), timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def record_proxy_traffic(
        self,
        project_id: str,
        *,
        intent_id: str | None,
        request: str,
        response: str | None = None,
        baseline: str | None = None,
        status: str = "recorded",
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/verify/proxy_traffic",
            json={
                "intent_id": intent_id,
                "request": request,
                "response": response,
                "baseline": baseline,
                "status": status,
            },
        )

    def conclude_observations(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        observations: list[dict],
        base_knowledge_patches: list[dict] | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={
                "worker": worker,
                "observations": observations,
                "base_knowledge_patches": base_knowledge_patches or [],
            },
        )

    def bootstrap_auth_deployment(self, snapshot: dict[str, Any]) -> ApiResult:
        """Apply the Dispatcher-owned auth deployment snapshot on the Server."""
        return self._request_json(
            "POST",
            "/internal/auth/deployment",
            json=snapshot,
        )


    def create_auth_request_internal(
        self,
        project_id: str,
        source_fact_ids: list[str],
        auth_ref: str,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            "/internal/auth/requests",
            json={
                "project_id": project_id,
                "source_fact_ids": source_fact_ids,
                "auth_ref": auth_ref,
            },
        )


    def claim_auth_event(self, dispatcher_id: str) -> ApiResult:
        return self._request_json(
            "POST", "/internal/auth/events/claim", json={"dispatcher_id": dispatcher_id}
        )


    def apply_auth_event(
        self,
        event_id: str,
        dispatcher_id: str,
        *,
        operation: str,
        outcome_code: str | None = None,
    ) -> ApiResult:
        body: dict[str, Any] = {
            "event_id": event_id,
            "dispatcher_id": dispatcher_id,
            "operation": operation,
        }
        if outcome_code is not None:
            body["outcome_code"] = outcome_code
        return self._request_json("POST", "/internal/auth/events/apply", json=body)


    def recover_auth_events(self) -> ApiResult:
        return self._request_json("POST", "/internal/auth/events/recover", json={})


    def expire_auth_requests(self) -> ApiResult:
        return self._request_json("POST", "/internal/auth/requests/expire", json={})


    def create_auth_graph_intent(
        self,
        project_id: str,
        source_key: str,
        source_fact_ids: list[str],
        description: str,
        *,
        creator: str = "operator.auth",
        worker: str = "operator.auth",
    ) -> ApiResult:
        return self._request_json(
            "POST",
            "/internal/auth/graph/intents",
            json={
                "project_id": project_id,
                "source_key": source_key,
                "source_fact_ids": source_fact_ids,
                "description": description,
                "creator": creator,
                "worker": worker,
            },
        )


    def get_auth_graph_intent(self, project_id: str, source_key: str) -> ApiResult:
        return self._request_json(
            "GET", f"/internal/auth/graph/intents/{project_id}", json={"source_key": source_key}
        )


    def conclude_auth_graph_intent(
        self,
        project_id: str,
        intent_source_key: str,
        fact_source_key: str,
        worker: str,
        description: str,
    ) -> ApiResult:
        result = self._request_json(
            "POST",
            "/internal/auth/graph/conclude",
            json={
                "project_id": project_id,
                "intent_source_key": intent_source_key,
                "fact_source_key": fact_source_key,
                "worker": worker,
                "description": description,
            },
        )
        if isinstance(result.data, dict):
            fact = result.data.get("fact")
            fact_id = result.data.get("fact_id") or (fact.get("id") if isinstance(fact, dict) else None)
            if fact_id:
                self._last_auth_graph_fact_id = str(fact_id)
        return result


    def acknowledge_auth_graph_outbox(
        self,
        event_id: str,
        dispatcher_id: str,
        *,
        state: str,
        intent_id: str | None = None,
        fact_id: str | None = None,
        outcome_code: str | None = None,
    ) -> ApiResult:
        body: dict[str, Any] = {
            "event_id": event_id,
            "dispatcher_id": dispatcher_id,
            "state": state,
        }
        if intent_id is not None:
            body["intent_id"] = intent_id
        if fact_id is not None:
            body["fact_id"] = fact_id
        if outcome_code is not None:
            body["outcome_code"] = outcome_code
        return self._request_json("POST", "/internal/auth/graph/outbox/ack", json=body)


    def create_auth_request(
        self,
        project_id: str,
        source_fact_ids: list[str],
        auth_ref: str,
        role: str,
        reason: str,
        login_url: str | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/auth-requests",
            json={
                "source_fact_ids": source_fact_ids,
                "auth_ref": auth_ref,
                "role": role,
                "reason": reason,
                "login_url": login_url,
            },
        )


    def list_auth_requests(self, status: str | None = None) -> ApiResult:
        path = "/auth-requests"
        if status is not None:
            path = f"{path}?status={status}"
        return self._request_json("GET", path, json={})


    def list_auth_helper_pending(self, project_id: str) -> ApiResult:
        """Read the narrow project-scoped view intended for a desktop Helper."""
        return self._request_json(
            "GET", f"/projects/{project_id}/auth-requests/helper-pending", json={}
        )


    def get_auth_helper_view(self, project_id: str, request_id: str) -> ApiResult:
        return self._request_json(
            "GET", f"/projects/{project_id}/auth-requests/{request_id}/helper-view", json={}
        )


    def create_auth_event(self, body: dict[str, Any]) -> ApiResult:
        """Enqueue one closed Helper/CLI event for Dispatcher consumption."""
        return self._request_json("POST", "/auth-events", json=body)


    def _request_json(
        self,
        method: str,
        path: str,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> ApiResult:
        request_headers = dict(headers or {})
        token = self._server_token
        if token and (path.startswith("/auth") or "/auth-" in path or path.endswith("/auth-deployment")):
            request_headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                headers=request_headers or None,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        bearer = ADMIN_TOKEN or getattr(self, "_server_token", None)
        if bearer:
            session.headers["Authorization"] = f"Bearer {bearer}"
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
