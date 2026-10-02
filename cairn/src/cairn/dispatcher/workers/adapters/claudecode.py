from __future__ import annotations

import json
import math
import re
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import AnalysisResponse, DriverResult, SeedSessionDriver, TrajectoryStep
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


ANTHROPIC_VERSION = "2023-06-01"
ANSI_MODEL_FRAGMENT = re.compile(r"(?:\x1b)?\[[0-9;?]*[ -/]*[@-~]")


def _clean_model_name(value: object) -> str:
    return ANSI_MODEL_FRAGMENT.sub("", str(value)).strip()


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def local_binary(self) -> str | None:
        return "claude"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={
                "Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": env["ANTHROPIC_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['ANTHROPIC_BASE_URL']}/v1/messages (model={worker.env['ANTHROPIC_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                *self.model_args(worker),
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "-p",
            ],
            session=session,
            stdin=prompt,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> DriverResult:
        return DriverResult(
            argv=[
                "claude",
                *self.model_args(worker),
                "-r",
                session,
                "--dangerously-skip-permissions",
                "-p",
            ],
            session=session,
            stdin=prompt,
        )

    def extract_analysis_response(self, stdout: str, stderr: str) -> AnalysisResponse:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("Claude Code analysis did not return JSON output metadata") from exc
        if not isinstance(payload, dict):
            raise ValueError("Claude Code analysis output must be a JSON object")
        if payload.get("is_error") is True:
            raise ValueError("Claude Code reported an analysis error")
        structured = payload.get("structured_output")
        result = (
            json.dumps(structured, ensure_ascii=False)
            if isinstance(structured, dict)
            else payload.get("result")
        )
        if not isinstance(result, str) or not result.strip():
            raise ValueError("Claude Code analysis output is missing the result text")

        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        metadata: dict[str, Any] = {"provider": "claude-code"}
        for source_key, target_key in (
            ("duration_ms", "provider_duration_ms"),
            ("duration_api_ms", "provider_api_duration_ms"),
            ("num_turns", "num_turns"),
        ):
            value = payload.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[target_key] = value
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[key] = value
        cost = payload.get("total_cost_usd")
        if (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(float(cost))
            and cost >= 0
        ):
            metadata["total_cost_usd"] = float(cost)

        model_usage = payload.get("modelUsage")
        model_names = (
            sorted({cleaned for name in model_usage if (cleaned := _clean_model_name(name))})
            if isinstance(model_usage, dict)
            else []
        )
        model = ",".join(model_names)[:256] or None
        return AnalysisResponse(text=result, metadata=metadata, model=model)

    def extract_trajectory(self, session_data: str) -> list[TrajectoryStep]:
        steps: list[TrajectoryStep] = []
        pending_calls: dict[str, dict[str, Any]] = {}
        step_id = 0
        for line in session_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            msg = event.get("message", event)
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "assistant" and isinstance(content, list):
                thinking_text = ""
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type == "thinking":
                        thinking_text = item.get("thinking", "")
                    elif item_type == "text":
                        thinking_text = thinking_text or item.get("text", "")
                    elif item_type == "tool_use":
                        call_id = item.get("id", "")
                        tool_input = item.get("input", {})
                        if isinstance(tool_input, dict):
                            action = (
                                tool_input.get("command", "")
                                or tool_input.get("content", "")
                                or json.dumps(tool_input, ensure_ascii=False)[:2000]
                            )
                        else:
                            action = str(tool_input)[:2000]
                        pending_calls[str(call_id)] = {
                            "name": item.get("name", ""),
                            "action": action,
                            "thinking": thinking_text,
                        }
                        thinking_text = ""
            elif role in ("tool_result", "tool"):
                call_id = str(msg.get("tool_use_id", "") or msg.get("toolCallId", ""))
                if isinstance(content, str):
                    observation = content[:8000]
                elif isinstance(content, list):
                    observation = "\n".join(
                        item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
                    )[:8000]
                else:
                    observation = ""
                call_info = pending_calls.pop(call_id, None)
                if call_info:
                    step_id += 1
                    steps.append(
                        TrajectoryStep(
                            step_id=step_id,
                            action=call_info["action"],
                            observation=observation,
                            tool_type=call_info["name"],
                            thinking=call_info.get("thinking") or None,
                        )
                    )
        return steps
