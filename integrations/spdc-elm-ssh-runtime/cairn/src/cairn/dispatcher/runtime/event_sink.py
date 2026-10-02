from __future__ import annotations

import logging
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.redaction import redact_text
from cairn.shared.worker_events import WorkerEvent, message_event, status_event

LOG = logging.getLogger(__name__)
RAW_PREVIEW_TOTAL_BYTES = 16 * 1024
RAW_PREVIEW_EVENT_BYTES = 4 * 1024
MAX_APPEND_EVENTS = 250


@dataclass(slots=True)
class FlushResult:
    ok: bool
    status_code: int | None = None
    text: str = ""
    attempts: int = 0
    batch_id: str | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass(slots=True)
class ExecutionEventSink:
    client: CairnClient
    execution_id: str
    dispatcher_id: str = "dispatcher"
    sink_token: str | None = None
    secrets: list[str] = field(default_factory=list)
    batch_size: int = 25
    max_queue_events: int = 1000
    flush_interval_seconds: float = 1.0
    live_flush: bool = True
    append_retry_delays: tuple[float, ...] = (0.25, 1.0, 3.0)
    close_timeout_seconds: float = 30.0
    event_projector: Any | None = None
    raw_ref: dict[str, Any] | None = None
    _lock: Any = field(init=False, repr=False)
    _sink_id: str = field(init=False, repr=False)
    _seq: int = field(default=0, init=False, repr=False)
    _queue: list[WorkerEvent] = field(default_factory=list, init=False, repr=False)
    _failed_flushes: int = field(default=0, init=False, repr=False)
    _projector_failed: bool = field(default=False, init=False, repr=False)
    _projector_closed: bool = field(default=False, init=False, repr=False)
    _projected_assistant_messages: int = field(default=0, init=False, repr=False)
    _batch_seq: int = field(default=0, init=False, repr=False)
    _last_successful_flush_at: float | None = field(default=None, init=False, repr=False)
    _oldest_queued_at: float | None = field(default=None, init=False, repr=False)
    _fatal_error: str | None = field(default=None, init=False, repr=False)
    _terminal_finished: bool = field(default=False, init=False, repr=False)
    _raw_chunk_ordinals: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0}, init=False, repr=False)
    _raw_preview: "RawPreviewBudgeter" = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        import hashlib as _hashlib
        token_hash = _hashlib.sha256((self.sink_token or "").encode()).hexdigest()[:8]
        self._sink_id = f"{self.execution_id}:sink:{token_hash}"
        self._raw_preview = RawPreviewBudgeter(self.execution_id, raw_ref=self.raw_ref)

    @property
    def failed_flushes(self) -> int:
        return self._failed_flushes

    @property
    def last_successful_flush_at(self) -> float | None:
        return self._last_successful_flush_at

    @property
    def oldest_queued_at(self) -> float | None:
        return self._oldest_queued_at

    @property
    def has_projected_assistant_message(self) -> bool:
        return self._projected_assistant_messages > 0

    def write_status(self, status: str, **payload) -> None:
        self.write_event(status_event(status, event_key=self._event_key("status"), **payload))
        if self.live_flush and status == "running":
            patch_execution = getattr(self.client, "patch_execution", None)
            if callable(patch_execution):
                patch = {"dispatcher_id": self.dispatcher_id, "status": "running"}
                if self.sink_token is not None:
                    patch["sink_token"] = self.sink_token
                response = patch_execution(
                    self.execution_id,
                    patch,
                )
                if not response.ok:
                    LOG.warning(
                        "running status patch failed execution=%s status=%s body=%s",
                        self.execution_id,
                        response.status_code,
                        response.text,
                    )
            self.flush()

    def write_stream(self, stream: str, text: str) -> None:
        if stream not in {"stdout", "stderr"} or not text:
            return
        redacted = redact_text(text, self.secrets)
        self._write_projected_stream_events(stream, redacted)
        for payload in self._raw_preview.preview_events(stream, redacted):
            self.write_event(WorkerEvent(event_type=stream, payload=payload, event_key=self._raw_event_key(stream)))

    def write_event(self, event: WorkerEvent) -> None:
        if self._fatal_error is not None and event.event_type != "message":
            return
        should_flush = False
        with self._lock:
            if not self._queue:
                self._oldest_queued_at = time.monotonic()
            self._queue.append(event)
            should_flush = self.live_flush and (len(self._queue) >= self.batch_size or self._should_time_flush_locked())
            over_limit = len(self._queue) > self.max_queue_events
        if over_limit:
            result = self.flush()
            if not result:
                self._mark_fatal("queue_full", result)
                return
        if should_flush:
            self.flush()

    def flush(self) -> FlushResult:
        final = FlushResult(ok=True, attempts=0)
        while True:
            result = self._flush_one_batch()
            final = result
            if not result.ok:
                return result
            with self._lock:
                if not self._queue:
                    return final

    def _flush_one_batch(self) -> FlushResult:
        with self._lock:
            if not self._queue:
                return FlushResult(ok=True, attempts=0)
            events = self._queue[:MAX_APPEND_EVENTS]
            self._queue = self._queue[MAX_APPEND_EVENTS:]
            self._oldest_queued_at = time.monotonic() if self._queue else None
            batch_id = self._next_batch_id()
        result = self._append_batch(events, batch_id=batch_id)
        if result.ok:
            self._last_successful_flush_at = time.monotonic()
            return result
        if result.status_code in {409, 422}:
            with self._lock:
                self._queue = []
                self._oldest_queued_at = None
                self._failed_flushes += 1
            fatal_code = "event_key_conflict" if result.status_code == 409 else "validation_too_long"
            failure_kind = "append_conflict" if result.status_code == 409 else "validation_too_long"
            self._mark_fatal(fatal_code, result)
            LOG.warning(
                "event_append_failed execution=%s sink_id=%s batch_id=%s failure_kind=%s "
                "status=%s event_count=%d first_event_key=%s",
                self.execution_id,
                self._sink_id,
                batch_id,
                failure_kind,
                result.status_code,
                len(events),
                events[0].event_key if events else None,
            )
            return result
        with self._lock:
            self._queue = events + self._queue
            self._oldest_queued_at = self._oldest_queued_at or time.monotonic()
            self._failed_flushes += 1
        failure_kind = "append_http_timeout" if result.status_code == 0 else "append_http_error"
        LOG.warning(
            "event_append_failed execution=%s sink_id=%s batch_id=%s failure_kind=%s "
            "status=%s queue_depth=%d event_count=%d attempt=%d",
            self.execution_id,
            self._sink_id,
            batch_id,
            failure_kind,
            result.status_code,
            len(self._queue),
            len(events),
            result.attempts,
        )
        return result

    def _append_batch(self, events: list[WorkerEvent], *, batch_id: str) -> FlushResult:
        attempts = 0
        delays = (0.0, *self.append_retry_delays)
        last_response = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            attempts += 1
            response = self.client.append_execution_events(
                self.execution_id,
                dispatcher_id=self.dispatcher_id,
                sink_token=self.sink_token,
                events=[event.to_api_payload() for event in events],
            )
            last_response = response
            if response.ok:
                if attempts > 1:
                    LOG.info("idempotent_replay execution=%s batch_id=%s attempts=%s", self.execution_id, batch_id, attempts)
                return FlushResult(ok=True, status_code=response.status_code, text=response.text, attempts=attempts, batch_id=batch_id)
            if response.status_code == 409:
                LOG.warning("event_key_conflict execution=%s batch_id=%s body=%s", self.execution_id, batch_id, response.text)
                break
            if response.status_code != 0:
                break
        assert last_response is not None
        return FlushResult(
            ok=False,
            status_code=last_response.status_code,
            text=last_response.text,
            attempts=attempts,
            batch_id=batch_id,
        )

    def _finish_batch(self, events: list[WorkerEvent], patch: dict[str, Any], *, batch_id: str) -> FlushResult:
        finish = getattr(self.client, "finish_execution", None)
        if callable(finish):
            response = finish(
                self.execution_id,
                dispatcher_id=self.dispatcher_id,
                sink_token=self.sink_token,
                events=[event.to_api_payload() for event in events],
                patch=patch,
            )
            if response.ok:
                self._last_successful_flush_at = time.monotonic()
                return FlushResult(ok=True, status_code=response.status_code, text=response.text, attempts=1, batch_id=batch_id)
            LOG.warning(
                "finish_timeout execution=%s batch_id=%s status=%s body=%s",
                self.execution_id,
                batch_id,
                response.status_code,
                response.text,
            )
            return FlushResult(ok=False, status_code=response.status_code, text=response.text, attempts=1, batch_id=batch_id)

        response = self.client.append_execution_events(
            self.execution_id,
            dispatcher_id=self.dispatcher_id,
            sink_token=self.sink_token,
            events=[event.to_api_payload() for event in events],
        )
        if not response.ok:
            return FlushResult(ok=False, status_code=response.status_code, text=response.text, attempts=1, batch_id=batch_id)
        patch_payload = {"dispatcher_id": self.dispatcher_id, **patch}
        if self.sink_token is not None:
            patch_payload["sink_token"] = self.sink_token
        patch_response = self.client.patch_execution(self.execution_id, patch_payload)
        return FlushResult(
            ok=patch_response.ok,
            status_code=patch_response.status_code,
            text=patch_response.text,
            attempts=1,
            batch_id=batch_id,
        )

    def close(
        self,
        *,
        terminal_status: str | None = None,
        returncode: int | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        patch_fields: dict[str, Any] | None = None,
    ) -> bool:
        if terminal_status is not None and self._terminal_finished:
            return True
        self.flush_projector_events()
        self._write_projected_events(self._raw_preview.close_events())
        if terminal_status is not None:
            prefix_flush = self._flush_until_terminal_batch_fits()
            if not prefix_flush.ok:
                self._mark_terminal_barrier_failed(prefix_flush)
                return False
            if self._fatal_error is not None and terminal_status == "succeeded":
                terminal_status = "failed"
                error_code = error_code or "event_flush_failed"
                error_detail = error_detail or self._fatal_error
            patch = {
                "status": terminal_status,
                "returncode": returncode,
                "error_code": error_code,
                "error_detail": error_detail,
            }
            if patch_fields:
                patch.update(patch_fields)
            self._enqueue_now(status_event(terminal_status, event_key=self._event_key("status"), error_code=error_code, error_detail=error_detail))
            with self._lock:
                events = self._queue
                self._queue = []
                self._oldest_queued_at = None
                batch_id = self._next_batch_id()
            result = self._finish_batch(events, patch, batch_id=batch_id)
            if result.ok:
                self._terminal_finished = True
                return True
            with self._lock:
                self._queue = events + self._queue
                self._oldest_queued_at = self._oldest_queued_at or time.monotonic()
                self._failed_flushes += 1
            self._mark_terminal_barrier_failed(result)
            return False
        return bool(self.flush())

    def _flush_until_terminal_batch_fits(self) -> FlushResult:
        while True:
            with self._lock:
                if len(self._queue) < MAX_APPEND_EVENTS:
                    return FlushResult(ok=True, attempts=0)
            result = self._flush_one_batch()
            if not result.ok:
                return result

    def _event_key(self, prefix: str) -> str:
        with self._lock:
            self._seq += 1
            return f"{self.execution_id}:{prefix}:{self._seq}"

    def _raw_event_key(self, stream: str) -> str:
        with self._lock:
            self._raw_chunk_ordinals[stream] += 1
            return f"{self.execution_id}:raw:{stream}:{self._raw_chunk_ordinals[stream]}"

    def _next_batch_id(self) -> str:
        self._batch_seq += 1
        return f"{self.execution_id}:batch:{self._batch_seq}"

    def _should_time_flush_locked(self) -> bool:
        if self._oldest_queued_at is None:
            return False
        if self._last_successful_flush_at is None:
            return False
        return (time.monotonic() - self._oldest_queued_at) >= self.flush_interval_seconds

    def _enqueue_now(self, event: WorkerEvent) -> None:
        with self._lock:
            if not self._queue:
                self._oldest_queued_at = time.monotonic()
            self._queue.append(event)

    def _mark_fatal(self, code: str, result: FlushResult) -> None:
        if self._fatal_error is not None:
            return
        self._fatal_error = code
        LOG.warning(
            "event_sink_fatal execution=%s sink_id=%s batch_id=%s failure_kind=%s queue_length=%s",
            self.execution_id,
            self._sink_id,
            result.batch_id,
            code,
            len(self._queue),
        )
        self._enqueue_now(
            WorkerEvent(
                event_type="metric",
                payload={
                    "metric": "event_sink_diagnostic",
                    "diagnostic": True,
                    "failure_kind": code,
                    "status_code": result.status_code,
                    "batch_id": result.batch_id,
                    "detail": result.text[:2000],
                },
                event_key=self._event_key(f"diagnostic-{code}"),
            )
        )

    def _mark_terminal_barrier_failed(self, result: FlushResult) -> None:
        LOG.warning(
            "terminal_barrier_failed execution=%s batch_id=%s status=%s body=%s",
            self.execution_id,
            result.batch_id,
            result.status_code,
            result.text,
        )
        patch = {
            "dispatcher_id": self.dispatcher_id,
            "status": "failed",
            "error_code": "event_flush_failed",
            "error_detail": result.text or f"terminal barrier failed status={result.status_code}",
        }
        if self.sink_token is not None:
            patch["sink_token"] = self.sink_token
        response = self.client.patch_execution(self.execution_id, patch)
        if not response.ok:
            LOG.warning(
                "terminal barrier fallback patch failed execution=%s status=%s body=%s",
                self.execution_id,
                response.status_code,
                response.text,
            )

    def _write_projected_stream_events(self, stream: str, text: str) -> None:
        if self.event_projector is None or self._projector_failed or self._projector_closed:
            return
        try:
            events = self.event_projector.feed(stream, text)
        except Exception as exc:
            self._projector_failed = True
            self.write_event(
                message_event(
                    "system",
                    f"dispatcher parser diagnostic: worker output projector failed: {exc}",
                    event_key=self._event_key("parser-diagnostic"),
                )
            )
            return
        self._write_projected_events(events or [])

    def flush_projector_events(self) -> None:
        if self.event_projector is None or self._projector_failed or self._projector_closed:
            return
        try:
            events = self.event_projector.close()
            self._projector_closed = True
        except Exception as exc:
            self._projector_failed = True
            self.write_event(
                message_event(
                    "system",
                    f"dispatcher parser diagnostic: worker output projector failed during close: {exc}",
                    event_key=self._event_key("parser-diagnostic"),
                )
            )
            return
        self._write_projected_events(events or [])

    def _write_projected_events(self, events: list[WorkerEvent]) -> None:
        for event in events:
            if event.event_type == "message" and event.role == "assistant":
                self._projected_assistant_messages += 1
            self.write_event(event)


@dataclass(slots=True)
class RawPreviewBudgeter:
    execution_id: str
    raw_ref: dict[str, Any] | None = None
    per_stream_limit: int = RAW_PREVIEW_TOTAL_BYTES
    per_event_limit: int = RAW_PREVIEW_EVENT_BYTES
    _persisted: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    _seen: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    _omitted: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    _events_omitted: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    _sanitised_summary_emitted: dict[str, bool] = field(default_factory=lambda: {"stdout": False, "stderr": False})
    _metric_omitted_emitted: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    _seq: int = 0

    def preview_events(self, stream: str, text: str) -> list[dict[str, Any]]:
        raw_bytes = len(text.encode("utf-8"))
        self._seen[stream] += raw_bytes
        sanitised_text = _sanitise_raw_preview_text(text)
        remaining = self.per_stream_limit - self._persisted[stream]
        if remaining <= 0:
            self._omitted[stream] += raw_bytes
            self._events_omitted[stream] += 1
            summary = self._sanitised_summary_event(stream, sanitised_text)
            if summary is not None:
                return [summary]
            return []
        preview_text, preview_bytes, _omitted_preview_bytes = _truncate_utf8(sanitised_text, min(remaining, self.per_event_limit))
        if not preview_text and raw_bytes:
            self._omitted[stream] += raw_bytes
            self._events_omitted[stream] += 1
            return []
        self._persisted[stream] += preview_bytes
        total_omitted = max(0, raw_bytes - preview_bytes)
        self._omitted[stream] += total_omitted
        if total_omitted:
            self._events_omitted[stream] += 1
        return [
            {
                "text": preview_text,
                "preview": True,
                "truncated": total_omitted > 0 or self._seen[stream] > self._persisted[stream],
                "omitted_bytes": self._omitted[stream],
                "raw_ref": self.raw_ref or {"kind": "execution_metadata", "key": "raw_stream"},
                "sanitised": preview_text != text,
            }
        ]

    def _sanitised_summary_event(self, stream: str, sanitised_text: str) -> dict[str, Any] | None:
        if self._sanitised_summary_emitted[stream] or "messages_omitted" not in sanitised_text:
            return None
        preview_text, preview_bytes, _ = _truncate_utf8(sanitised_text, self.per_event_limit)
        self._persisted[stream] += preview_bytes
        self._sanitised_summary_emitted[stream] = True
        return {
            "text": preview_text,
            "preview": True,
            "truncated": True,
            "omitted_bytes": self._omitted[stream],
            "raw_ref": self.raw_ref or {"kind": "execution_metadata", "key": "raw_stream"},
            "sanitised": True,
        }

    def close_events(self) -> list[WorkerEvent]:
        events: list[WorkerEvent] = []
        for stream in ("stdout", "stderr"):
            if (
                self._seen[stream] == 0
                or self._omitted[stream] == 0
                or self._omitted[stream] <= self._metric_omitted_emitted[stream]
            ):
                continue
            self._metric_omitted_emitted[stream] = self._omitted[stream]
            events.append(
                WorkerEvent(
                    event_type="metric",
                    payload={
                        "metric": "raw_stream_storage",
                        "stream": stream,
                        "raw_bytes_seen": self._seen[stream],
                        "raw_bytes_persisted_to_db": self._persisted[stream],
                        "raw_events_omitted": self._events_omitted[stream],
                        "raw_ref": self.raw_ref or {"kind": "execution_metadata", "key": "raw_stream"},
                    },
                    event_key=f"{self.execution_id}:raw-storage:{stream}:final",
                )
            )
        return events


def _truncate_utf8(text: str, limit: int) -> tuple[str, int, int]:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, len(data), 0
    truncated = data[:limit].decode("utf-8", errors="ignore")
    return truncated, len(truncated.encode("utf-8")), len(data) - len(truncated.encode("utf-8"))


def _sanitise_raw_preview_text(text: str) -> str:
    lines: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        suffix = ""
        body = line
        if line.endswith("\n"):
            body = line[:-1]
            suffix = "\n"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        if isinstance(payload, dict) and payload.get("type") == "agent_end" and isinstance(payload.get("messages"), list):
            messages = payload.pop("messages")
            encoded = json.dumps(messages, ensure_ascii=False)
            payload["messages_omitted"] = True
            payload["messages_count"] = len(messages)
            payload["messages_omitted_bytes"] = len(encoded.encode("utf-8"))
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + suffix
            changed = True
        lines.append(line)
    return "".join(lines) if changed else text


class CompositeRunLogger:
    def __init__(self, *loggers):
        self._loggers = [logger for logger in loggers if logger is not None]

    def write_stream(self, stream: str, text: str) -> None:
        for logger in self._loggers:
            write_stream = getattr(logger, "write_stream", None)
            if callable(write_stream):
                write_stream(stream, text)

    def finish(self, *, returncode: int, timed_out: bool, cancelled: bool, cancel_reason: str | None) -> None:
        for logger in self._loggers:
            finish = getattr(logger, "finish", None)
            if callable(finish):
                finish(
                    returncode=returncode,
                    timed_out=timed_out,
                    cancelled=cancelled,
                    cancel_reason=cancel_reason,
                )
