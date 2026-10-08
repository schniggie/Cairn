from __future__ import annotations

import json
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


_SESSION_ID_FIELDS = ("sessionID",)
_ENVELOPE_KEYS = ("part",)
_TEXT_PART_TYPE = "text"
_DEFAULT_PROVIDER_NPM = "@ai-sdk/openai-compatible"
_PROVIDER_KEYS = ("OPENCODE_MODEL", "OPENCODE_BASE_URL", "OPENCODE_API_KEY")


class OpenCodeDriver(WorkerDriver):
    """Adapter for the OpenCode CLI.

    Container mode injects an OpenAI-compatible provider from worker env.
    Local mode can omit those keys and use the host OpenCode login instead.
    The prompt stays on argv because `opencode run` blocks when stdin is a pipe.
    """

    type_name = "opencode"

    def local_binary(self) -> str | None:
        return "opencode"

    def describe_health(self, worker: WorkerConfig) -> str:
        if self._provider_ready(worker):
            return f"POST {worker.env['OPENCODE_BASE_URL']}/chat/completions (model={worker.env['OPENCODE_MODEL']})"
        return "opencode CLI host authentication; no remote ping"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        if not self._provider_ready(worker):
            return HealthResult(
                ok=True,
                status=None,
                detail="opencode CLI uses host authentication; no remote ping",
            )
        env = worker.env
        return http_ping(
            f"{env['OPENCODE_BASE_URL'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {env['OPENCODE_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["OPENCODE_MODEL"],
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._argv(worker, prompt, session), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> DriverResult:
        return DriverResult(argv=self._argv(worker, prompt, session), session=session)

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            found = self._find_session_id(event)
            if found:
                return found
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        parts: list[str] = []
        for event in self._iter_events(stdout):
            parts.extend(self._collect_text(event))
        return "\n".join(parts).strip() or stdout

    def _argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        opencode_argv = self._run_argv(worker, prompt, session)
        if self._provider_ready(worker):
            return self._wrap(worker, opencode_argv)
        return ["opencode", *opencode_argv]

    @staticmethod
    def _provider_ready(worker: WorkerConfig) -> bool:
        return all(worker.env.get(key, "").strip() for key in _PROVIDER_KEYS)

    @staticmethod
    def _run_argv(worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        model = worker.model or worker.env.get("OPENCODE_MODEL", "").strip()
        argv = ["run", "--pure", "--format", "json", "--dangerously-skip-permissions"]
        if model:
            argv.extend(["-m", f"cairn/{model}" if OpenCodeDriver._provider_ready(worker) else model])
        if session:
            argv.extend(["-s", session])
        argv.extend(["--", prompt])
        return argv

    def _wrap(self, worker: WorkerConfig, opencode_argv: list[str]) -> list[str]:
        script = (
            'cfg="$1"\n'
            "shift 1\n"
            'exec env OPENCODE_CONFIG_CONTENT="$cfg" '
            "OPENCODE_DISABLE_AUTOUPDATE=1 "
            "OPENCODE_DISABLE_MODELS_FETCH=1 "
            'opencode "$@" </dev/null\n'
        )
        return ["/bin/sh", "-lc", script, "--", self._config_json(worker), *opencode_argv]

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        text = stdout.strip()
        if not text:
            return []
        try:
            whole = json.loads(text)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, list):
            return [event for event in whole if isinstance(event, dict)]
        if isinstance(whole, dict):
            return [whole]
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @classmethod
    def _find_session_id(cls, event: dict[str, Any]) -> str | None:
        for key in _SESSION_ID_FIELDS:
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        for key in _ENVELOPE_KEYS:
            nested = event.get(key)
            if isinstance(nested, dict):
                found = cls._find_session_id(nested)
                if found:
                    return found
        return None

    @classmethod
    def _collect_text(cls, event: dict[str, Any]) -> list[str]:
        out: list[str] = []
        if event.get("type") == _TEXT_PART_TYPE:
            text = event.get("text")
            if isinstance(text, str) and text:
                out.append(text)
            part = event.get("part")
            if isinstance(part, dict):
                nested = part.get("text")
                if isinstance(nested, str) and nested and nested not in out:
                    out.append(nested)
        for key in _ENVELOPE_KEYS:
            nested = event.get(key)
            if isinstance(nested, dict) and event.get("type") != _TEXT_PART_TYPE:
                out.extend(cls._collect_text(nested))
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        out.extend(cls._collect_text(item))
        return out

    @staticmethod
    def _config_json(worker: WorkerConfig) -> str:
        env = worker.env
        model = worker.model or env["OPENCODE_MODEL"]
        npm = env.get("OPENCODE_PROVIDER_NPM") or _DEFAULT_PROVIDER_NPM
        payload: dict[str, Any] = {
            "provider": {
                "cairn": {
                    "npm": npm,
                    "options": {
                        "baseURL": env["OPENCODE_BASE_URL"],
                        "apiKey": env["OPENCODE_API_KEY"],
                    },
                    "models": {model: {"name": model}},
                }
            }
        }
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
