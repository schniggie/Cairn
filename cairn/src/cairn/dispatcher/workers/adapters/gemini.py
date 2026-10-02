from __future__ import annotations

import re

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver
from cairn.dispatcher.workers.health import HealthResult


class GeminiDriver(WorkerDriver):
    """Adapter for the Gemini CLI.

    The CLI authenticates on the host (`gemini auth` or GEMINI_API_KEY). Session
    resume is not exposed, so conclude fallback stays disabled.
    """

    type_name = "gemini"
    session_pattern = re.compile(r"[Cc]onversation\s+[Ii][Dd]:\s*(\S+)")

    def supports_conclude(self) -> bool:
        return False

    def local_binary(self) -> str | None:
        return "gemini"

    def describe_health(self, worker: WorkerConfig) -> str:
        return "gemini CLI host authentication; no remote ping"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        return HealthResult(
            ok=True,
            status=None,
            detail="gemini CLI uses host authentication; no remote ping",
        )

    def _model_args(self, worker: WorkerConfig) -> list[str]:
        model = worker.env.get("GEMINI_MODEL", "").strip()
        if not model:
            return []
        return ["--model", model]

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(
            argv=["gemini", *self._model_args(worker), "--yolo", "-p", prompt],
            session=None,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> DriverResult:
        return DriverResult(
            argv=["gemini", *self._model_args(worker), "--yolo", "-p", prompt],
            session=session,
        )

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        match = self.session_pattern.search(stderr)
        if match:
            return match.group(1)
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout
