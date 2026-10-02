from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

WorkerEventType = Literal["status", "stdout", "stderr", "message", "tool", "artifact", "fact_candidate", "session", "metric"]
WorkerEventRole = Literal["user", "assistant", "system", "tool"]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(slots=True)
class WorkerEvent:
    event_type: WorkerEventType
    payload: dict[str, Any] = field(default_factory=dict)
    role: WorkerEventRole | None = None
    event_key: str | None = None
    ts: str | None = None

    def to_api_payload(self) -> dict[str, Any]:
        if self.ts is None:
            self.ts = utcnow()
        return {
            "event_type": self.event_type,
            "role": self.role,
            "payload": self.payload,
            "event_key": self.event_key,
            "ts": self.ts,
        }


def stream_event(stream: Literal["stdout", "stderr"], text: str, *, event_key: str | None = None) -> WorkerEvent:
    return WorkerEvent(event_type=stream, payload={"text": text}, event_key=event_key)


def status_event(status: str, *, event_key: str | None = None, **payload: Any) -> WorkerEvent:
    return WorkerEvent(event_type="status", payload={"status": status, **payload}, event_key=event_key)


def session_event(
    *,
    kind: str | None,
    session_id: str | None,
    status: str,
    event_key: str | None = None,
    capture_method: str | None = None,
) -> WorkerEvent:
    return WorkerEvent(
        event_type="session",
        payload={
            "kind": kind,
            "id": session_id,
            "status": status,
            "capture_method": capture_method,
        },
        event_key=event_key,
    )


def message_event(role: WorkerEventRole, text: str, *, event_key: str | None = None) -> WorkerEvent:
    return WorkerEvent(event_type="message", role=role, payload={"text": text}, event_key=event_key)
