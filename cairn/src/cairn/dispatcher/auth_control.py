from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import requests

from cairn.dispatcher.protocol.client import ApiResult, CairnClient, ProtocolError
from cairn.auth.graph import AuthGraphAdapter
from cairn.auth.store import AuthStore
from cairn.auth.verifier import AuthVerifier

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class DispatcherAuthControl:
    """Dispatcher-side queue consumer for authentication events.

    Authentication is the only component allowed to bridge the secret plane and
    the graph.  Every graph effect is source-keyed and acknowledged through the
    internal Server control API so a crash can safely resume it.
    """

    client: CairnClient
    dispatcher_id: str = "dispatcher"
    auth_store: AuthStore | None = None
    auth_config: Any | None = None
    verifier: AuthVerifier | None = None

    def __post_init__(self) -> None:
        if self.auth_store is None and self.auth_config is not None:
            root = getattr(self.auth_config, "store_root", None)
            if root:
                self.auth_store = AuthStore(Path(root))
        if self.verifier is None and self.auth_config is not None:
            timeout = int(getattr(self.auth_config, "verify_timeout", 30))
            self.verifier = AuthVerifier(timeout_ms=timeout * 1000)

    def recover(self) -> ApiResult:
        return self.client.recover_auth_events()

    def reap(self) -> ApiResult:
        return self.client.expire_auth_requests()

    @staticmethod
    def select_operation(event: dict[str, object]) -> tuple[str, str | None]:
        """Choose the only legal storage operation for a claimed event.

        This is intentionally Dispatcher-owned policy. The Server receives the
        selected operation and only checks that it is compatible with storage.
        """
        kind = event.get("kind")
        status = event.get("request_status")
        helper_actor = event.get("helper_actor_id")
        actor = event.get("actor_id")
        if "helper_actor_id" in event and (
            (kind != "launch_requested" and actor != helper_actor)
            or (kind == "launch_requested" and status != "pending" and actor != helper_actor)
        ):
            return "reject", "not_request_owner"
        if kind == "launch_requested" and status == "pending":
            return "bind_actor", None
        if kind == "browser_opened" and status == "claimed":
            return "mark_waiting_user", None
        if kind == "login_succeeded" and status in {"waiting_user", "verifying"}:
            if status == "verifying" and "verification_event_id" in event and event.get("verification_event_id") != event.get("id"):
                return "reject", "invalid_transition"
            return "begin_verification", None
        if kind == "login_failed" and status in {"claimed", "waiting_user", "verifying"}:
            return "mark_failed", "login_failed"
        return "reject", "invalid_transition"

    def consume_once(self) -> ApiResult | None:
        claimed = self.client.claim_auth_event(self.dispatcher_id)
        if claimed.status_code == 204:
            return None
        if not claimed.ok:
            raise ProtocolError(
                f"auth event claim failed with status {claimed.status_code}",
                claimed.status_code,
                claimed.text,
            )
        event = claimed.data or {}
        if not isinstance(event, dict):
            raise ProtocolError("auth event claim returned malformed payload", 502, claimed.text)
        event_id = event.get("id")
        if not event_id:
            raise ProtocolError("auth event claim returned malformed payload", 502, claimed.text)
        operation, outcome_code = self.select_operation(event)
        apply_kwargs: dict[str, str] = {"operation": operation}
        if outcome_code is not None:
            apply_kwargs["outcome_code"] = outcome_code
        result = self.client.apply_auth_event(event_id, self.dispatcher_id, **apply_kwargs)
        if not result.ok:
            LOG.warning("auth event apply failed event=%s status=%s body=%s", event_id, result.status_code, result.text)
        if result.ok and event.get("kind") == "login_succeeded" and operation == "begin_verification":
            return self._verify_and_publish(event, result)
        if result.ok and event.get("kind") == "login_failed" and operation == "mark_failed":
            return self._publish_invalid(event, "login_failed", result)
        return result

    def _verify_and_publish(self, event: dict[str, object], applied: ApiResult) -> ApiResult:
        """Verify one claimed capture and drain its durable graph outbox."""
        # A missing auth configuration is a deployment error, but do not expose
        # filesystem/configuration details to the graph or request lifecycle.
        if self.auth_store is None or self.verifier is None or self.auth_config is None:
            return self._publish_invalid(event, "store_unavailable", applied)
        project_id = str(event.get("project_id", ""))
        request_id = str(event.get("request_id", ""))
        auth_ref = str(event.get("auth_ref", ""))
        actor_id = str(event.get("actor_id", ""))
        generation = event.get("capture_generation")
        if not project_id or not request_id or not auth_ref or not actor_id or not isinstance(generation, int):
            return self._ack_failure(event, "capture_mismatch", applied)
        configured_actor = getattr(self.auth_config, "helper_actor_id", None)
        if configured_actor and actor_id != configured_actor:
            return self._publish_invalid(event, "capture_mismatch", applied)
        try:
            target = self.auth_config.target(auth_ref)
        except (KeyError, ValueError, TypeError):
            LOG.warning("auth target unavailable event=%s", event.get("id"))
            return self._publish_invalid(event, "unknown_target", applied)
        try:
            self.auth_store.validate_capture(
                project_id, auth_ref, request_id=request_id, actor_id=actor_id,
                capture_generation=generation,
            )
        except (FileNotFoundError, OSError):
            LOG.warning("auth capture store unavailable event=%s", event.get("id"))
            return self._publish_invalid(event, "store_unavailable", applied)
        except (ValueError, TypeError):
            LOG.warning("auth capture mismatch event=%s", event.get("id"))
            return self._publish_invalid(event, "capture_mismatch", applied)
        try:
            state = self.auth_store.load_state(project_id, auth_ref)
            verification = self.verifier.verify_storage_state(target, state)
        except (FileNotFoundError, OSError):
            LOG.warning("auth state store unavailable event=%s", event.get("id"))
            return self._publish_invalid(event, "store_unavailable", applied)
        except (ValueError, TypeError):
            LOG.warning("auth state malformed event=%s", event.get("id"))
            return self._publish_invalid(event, "capture_mismatch", applied)
        except Exception:
            LOG.exception("auth verification failed unexpectedly event=%s", event.get("id"))
            return self._publish_invalid(event, "verification_failed", applied)

        if verification.valid:
            adapter = AuthGraphAdapter(self.client)
            intent_key = f"auth-event:{event['id']}:intent"
            fact_key = f"auth-event:{event['id']}:fact"
            source_ids = event.get("source_fact_ids")
            if not isinstance(source_ids, list) or not source_ids:
                source_ids = ["origin"]
            try:
                intent_id = adapter.verified(
                    project_id, target, methods=verification.methods(), from_ids=source_ids,
                    intent_source_key=intent_key, fact_source_key=fact_key,
                )
                fact_id = self._fact_id_from_graph_result(event, intent_id, fact_key, adapter)
                return self._ack_success(event, intent_id, fact_id, applied)
            except (ProtocolError, RuntimeError, requests.RequestException) as exc:
                LOG.warning("auth verified graph effect pending event=%s error=%s", event.get("id"), exc)
                return ApiResult(503, text="auth graph effect pending")
        return self._publish_invalid(event, "verification_failed", applied)

    def _publish_invalid(self, event: dict[str, object], reason: str, applied: ApiResult) -> ApiResult:
        try:
            adapter = AuthGraphAdapter(self.client)
            intent_key = f"auth-event:{event['id']}:intent"
            fact_key = f"auth-event:{event['id']}:fact"
            source_ids = event.get("source_fact_ids")
            if not isinstance(source_ids, list) or not source_ids:
                source_ids = ["origin"]
            target = None
            if self.auth_config is not None:
                try:
                    target = self.auth_config.target(str(event.get("auth_ref", "")))
                except (KeyError, ValueError, TypeError):
                    target = None
            if target is None:
                intent_id = adapter.invalid_ref(
                    str(event.get("project_id", "")), str(event.get("auth_ref", "unknown")),
                    from_ids=source_ids, intent_source_key=intent_key, fact_source_key=fact_key,
                )
            else:
                intent_id = adapter.invalid(
                    str(event.get("project_id", "")), target,
                    evidence=reason, from_ids=source_ids,
                    intent_source_key=intent_key, fact_source_key=fact_key,
                )
            fact_id = self._fact_id_from_graph_result(event, intent_id, fact_key, adapter)
            return self._ack_invalid(event, intent_id, fact_id, reason, applied)
        except (AttributeError, ProtocolError, RuntimeError, requests.RequestException, KeyError, ValueError) as exc:
            LOG.warning("auth invalid graph effect pending event=%s error=%s", event.get("id"), exc)
            return ApiResult(503, text="auth graph effect pending")

    def _fact_id_from_graph_result(self, event: dict[str, object], intent_id: str, fact_key: str, adapter: AuthGraphAdapter) -> str:
        # The internal conclude response carries the stable fact id.  Recording
        # clients may only return an intent id; in production the RPC is strict.
        if getattr(adapter, "last_fact_id", None):
            return str(adapter.last_fact_id)
        if hasattr(self.client, "last_auth_graph_fact_id"):
            fact_id = self.client.last_auth_graph_fact_id
            if fact_id:
                return str(fact_id)
        response = getattr(self.client, "conclude_auth_graph_intent_result", None)
        if isinstance(response, dict) and response.get("fact_id"):
            return str(response["fact_id"])
        # CairnClient caches the last RPC result for this purpose.
        cached = getattr(self.client, "_last_auth_graph_fact_id", None)
        if cached:
            return str(cached)
        raise RuntimeError("auth graph conclude returned no fact id")

    def _ack_success(self, event: dict[str, object], intent_id: str, fact_id: str, applied: ApiResult) -> ApiResult:
        ack = self.client.acknowledge_auth_graph_outbox(
            str(event["id"]), self.dispatcher_id, state="fact_created",
            intent_id=intent_id, fact_id=fact_id,
        )
        return ack if not ack.ok else ack

    def _ack_invalid(self, event: dict[str, object], intent_id: str, fact_id: str, reason: str, applied: ApiResult) -> ApiResult:
        return self.client.acknowledge_auth_graph_outbox(
            str(event["id"]), self.dispatcher_id, state="fact_created",
            intent_id=intent_id, fact_id=fact_id, outcome_code=reason,
        )

    def run_cycle(self) -> list[ApiResult]:
        """Recover claims, expire requests, then drain currently eligible events."""
        results = [self.recover(), self.reap()]
        if any(not result.ok for result in results):
            return results
        while True:
            result = self.consume_once()
            if result is None:
                break
            results.append(result)
            if not result.ok:
                break
        return results
