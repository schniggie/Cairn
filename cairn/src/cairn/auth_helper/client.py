from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
import time

from cairn.dispatcher.protocol.client import CairnClient
from cairn.server.models import AuthRequest


@dataclass(slots=True)
class AuthHelperRequest:
    """The deliberately narrow request view consumed by a local Helper."""

    id: str
    project_id: str
    auth_ref: str
    login_url: str | None
    status: str
    # Narrow Helper views intentionally omit these legacy fields; defaults keep
    # existing notification adapters structurally compatible without exposing them.
    role: str | None = None
    reason: str | None = None
    helper_actor_id: str | None = None


@dataclass(slots=True)
class AuthHelperClient:
    """Thin wrapper over ``CairnClient`` exposing the auth-request operations the
    desktop helper needs. Kept separate so the helper can be unit-tested with a fake
    without importing the full protocol client.
    """

    client: CairnClient
    _event_times: dict[tuple[str, str, str, int | None], str] = field(
        default_factory=dict, init=False, repr=False
    )

    EVENT_KINDS = frozenset({"launch_requested", "browser_opened", "login_succeeded", "login_failed"})

    def list_pending(self, project_id: str) -> list[AuthHelperRequest]:
        """Read pending requests through project-scoped Helper projections only."""
        if not project_id or not project_id.strip():
            return []
        response = self.client.list_auth_helper_pending(project_id)
        if not response.ok or not isinstance(response.data, list):
            return []
        result: list[AuthHelperRequest] = []
        for item in response.data:
            if not isinstance(item, dict):
                continue
            try:
                result.append(
                    AuthHelperRequest(
                        id=str(item["id"]),
                        project_id=project_id,
                        auth_ref=str(item["auth_ref"]),
                        login_url=item.get("login_url"),
                        status=str(item["status"]),
                        helper_actor_id=item.get("helper_actor_id"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def get_helper_view(self, project_id: str, request_id: str) -> AuthHelperRequest | None:
        response = self.client.get_auth_helper_view(project_id, request_id)
        if not response.ok or not isinstance(response.data, dict):
            return None
        item = response.data
        try:
            return AuthHelperRequest(
                id=str(item["id"]), project_id=project_id, auth_ref=str(item["auth_ref"]),
                login_url=item.get("login_url"), status=str(item["status"]),
                helper_actor_id=item.get("helper_actor_id"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def wait_until_claimed(
        self,
        request: AuthRequest | AuthHelperRequest,
        *,
        actor_id: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.2,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            view = self.get_helper_view(request.project_id, request.id)
            if view is not None and view.status == "claimed":
                bound_actor = getattr(view, "helper_actor_id", None)
                if actor_id is None or bound_actor == actor_id:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(max(0.0, poll_interval), max(0.0, deadline - time.monotonic())))

    def submit_event(
        self,
        request: AuthRequest | AuthHelperRequest,
        kind: str,
        *,
        capture_generation: int | None = None,
        idempotency_key: UUID | None = None,
        occurred_at: str | None = None,
    ) -> bool:
        """Enqueue one constrained Helper/CLI event; never mutate request state."""
        if kind not in self.EVENT_KINDS:
            raise ValueError(f"unsupported auth event: {kind}")
        if kind == "login_succeeded" and capture_generation is None:
            raise ValueError("capture_generation is required for login_succeeded")
        if kind != "login_succeeded" and capture_generation is not None:
            raise ValueError("capture_generation is only valid for login_succeeded")
        event_identity = (request.project_id, request.id, kind, capture_generation)
        if event_identity not in self._event_times:
            self._event_times[event_identity] = occurred_at or datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        body: dict[str, Any] = {
            "project_id": request.project_id,
            "request_id": request.id,
            "auth_ref": request.auth_ref,
            "kind": kind,
            "idempotency_key": str(
                idempotency_key
                or uuid5(
                    NAMESPACE_URL,
                    f"cairn:auth-event:{request.project_id}:{request.id}:{kind}:{capture_generation or ''}",
                )
            ),
            "occurred_at": self._event_times[event_identity],
        }
        if capture_generation is not None:
            body["capture_generation"] = capture_generation
        return self.client.create_auth_event(body).ok

    def submit_event_fields(
        self,
        project_id: str,
        request_id: str,
        auth_ref: str,
        kind: str,
        *,
        capture_generation: int | None = None,
        idempotency_key: UUID | None = None,
    ) -> bool:
        request = AuthHelperRequest(request_id, project_id, auth_ref, None, "unknown")
        return self.submit_event(
            request, kind, capture_generation=capture_generation, idempotency_key=idempotency_key
        )

    def wait_until_claimed_fields(
        self,
        project_id: str,
        request_id: str,
        *,
        actor_id: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.2,
    ) -> bool:
        request = AuthHelperRequest(request_id, project_id, "", None, "pending")
        return self.wait_until_claimed(
            request,
            actor_id=actor_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )

    def wait_until_verifiable(
        self,
        request: AuthRequest | AuthHelperRequest,
        *,
        actor_id: str | None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Wait for a request state in which this actor may submit verification.

        ``browser_opened`` moves a request to ``waiting_user`` before the operator
        can run ``auth verify``.  A prior verification can also leave it in
        ``verifying`` while a retry is in flight.  The helper view is the authority
        for both state and ownership; never accept a merely matching request id.
        """
        if not actor_id:
            return False
        # ``login_succeeded`` is not legal from ``claimed``: the Dispatcher first
        # requires the accepted browser_opened transition to ``waiting_user``.
        allowed_statuses = {"waiting_user", "verifying"}
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            view = self.get_helper_view(request.project_id, request.id)
            if (
                view is not None
                and view.status in allowed_statuses
                and view.helper_actor_id == actor_id
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(max(0.0, poll_interval), max(0.0, deadline - time.monotonic())))

    def wait_until_verifiable_fields(
        self,
        project_id: str,
        request_id: str,
        *,
        actor_id: str | None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.2,
    ) -> bool:
        request = AuthHelperRequest(request_id, project_id, "", None, "unknown")
        return self.wait_until_verifiable(
            request,
            actor_id=actor_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
