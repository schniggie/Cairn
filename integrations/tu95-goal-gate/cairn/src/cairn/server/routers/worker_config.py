from __future__ import annotations

from collections.abc import Callable
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
import logging
import os
import subprocess
from pathlib import Path
from typing import Literal
import requests
import yaml


router = APIRouter(tags=["worker-config"])

CONFIG_PATH = Path(os.environ.get("CAIRN_DISPATCH_CONFIG", "dispatch.yaml"))
MASKED_SECRET = "********"
LOG = logging.getLogger(__name__)
DEFAULT_MODEL_REGISTRY = {
    "codex": {"items": ["gpt-5.1-codex"], "default": "gpt-5.1-codex"},
    "claudecode": {"items": ["claude-sonnet-4-5"], "default": "claude-sonnet-4-5"},
}
_reload_dispatcher: Callable[[], None] | None = None


Provider = Literal["codex", "claudecode"]
CodexAuthMode = Literal["local", "api_key"]


def set_dispatcher_reload_callback(callback: Callable[[], None] | None) -> None:
    global _reload_dispatcher
    _reload_dispatcher = callback


def _registry_path() -> Path:
    override = os.environ.get("CAIRN_WORKER_MODEL_REGISTRY")
    if override:
        return Path(override)
    return CONFIG_PATH.with_name(f"{CONFIG_PATH.stem}.models.yaml")


class WorkerConfigForm(BaseModel):
    provider: Provider = "codex"
    auth_mode: CodexAuthMode = "local"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    max_running: int = Field(default=8, ge=1, le=16)

    @field_validator("model", "base_url", "api_key")
    @classmethod
    def validate_trimmed(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_credentials(self) -> WorkerConfigForm:
        if self.provider == "claudecode":
            self.auth_mode = "api_key"
        if self.provider == "claudecode" or self.auth_mode == "api_key":
            if not self.model:
                raise ValueError("model is required")
            if not self.base_url:
                raise ValueError("base_url is required")
        return self


class WorkerConfigResponse(WorkerConfigForm):
    config_path: str
    restart_required: bool = False
    available_models: list[str] = Field(default_factory=list)
    default_model: str = ""


class WorkerModelRequest(BaseModel):
    provider: Provider = "codex"
    items: list[str] = Field(default_factory=list)
    default: str = ""

    @field_validator("items")
    @classmethod
    def _normalize_items(cls, items: list[str]) -> list[str]:
        result: list[str] = []
        for item in items:
            name = str(item).strip()
            if not name or name in result:
                continue
            result.append(name)
        return result

    @field_validator("default")
    @classmethod
    def _normalize_default(cls, value: str) -> str:
        return str(value).strip()

    @model_validator(mode="after")
    def _validate_default(self) -> "WorkerModelRequest":
        if self.default and self.default not in self.items:
            raise ValueError("default must be present in items")
        return self


class PingResponse(BaseModel):
    ok: bool
    status_code: int | None = None
    preview: str = ""


@router.get("/worker-config", response_model=WorkerConfigResponse)
def get_worker_config():
    config = _load_yaml()
    worker = (config.get("workers") or [{}])[0]
    existing_provider = worker.get("type")
    provider = existing_provider if existing_provider in ("codex", "claudecode") else "codex"
    env = worker.get("env") or {}
    available_models, default_model = _get_model_catalog(provider)

    if provider == "claudecode":
        auth_mode: CodexAuthMode = "api_key"
        model = env.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        base_url = env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        has_secret = bool(env.get("ANTHROPIC_AUTH_TOKEN"))
    else:
        auth_mode = env.get("CODEX_AUTH_MODE")
        if auth_mode not in ("local", "api_key"):
            auth_mode = "api_key" if env.get("OPENAI_API_KEY") else "local"
        # local 模式下不回填目录默认值：model 为空就是“跟随 CLI 默认”，UI 按选中语义如实展示
        model = env.get("CODEX_MODEL", "")
        if auth_mode == "api_key" and not model:
            model = "gpt-5.1-codex"
        base_url = env.get("CODEX_BASE_URL", "https://api.openai.com/v1" if auth_mode == "api_key" else "")
        has_secret = bool(env.get("OPENAI_API_KEY"))

    return WorkerConfigResponse(
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        base_url=base_url,
        api_key=MASKED_SECRET if has_secret and auth_mode == "api_key" else "",
        max_running=int(worker.get("max_running") or 8),
        config_path=str(CONFIG_PATH),
        restart_required=False,
        available_models=available_models,
        default_model=default_model,
    )


@router.put("/worker-config", response_model=WorkerConfigResponse)
def update_worker_config(body: WorkerConfigForm):
    existing = _load_yaml()
    api_key = "" if _uses_local_codex(body) else _resolve_secret(body, existing)
    if not _uses_local_codex(body) and not api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    config = _build_dispatch_config(body, api_key, existing)
    _write_yaml(config)
    restart_required = True
    if _reload_dispatcher is not None:
        try:
            _reload_dispatcher()
            restart_required = False
        except Exception:
            LOG.exception("failed to request dispatcher reload")
    return WorkerConfigResponse(
        **body.model_dump(exclude={"api_key"}),
        api_key=MASKED_SECRET if api_key else "",
        config_path=str(CONFIG_PATH),
        restart_required=restart_required,
        available_models=_get_model_catalog(body.provider)[0],
        default_model=_get_model_catalog(body.provider)[1],
    )


@router.get("/worker-models", response_model=WorkerModelRequest)
def list_worker_models(provider: Provider = "codex") -> WorkerModelRequest:
    models, default_model = _get_model_catalog(provider)
    return WorkerModelRequest(provider=provider, items=models, default=default_model)


@router.put("/worker-models", response_model=WorkerModelRequest)
def save_worker_models(body: WorkerModelRequest) -> WorkerModelRequest:
    registry = _load_model_registry()
    registry[body.provider] = {"items": body.items, "default": body.default}
    _write_model_registry(registry)
    return WorkerModelRequest(provider=body.provider, items=body.items, default=body.default)


@router.post("/worker-config/ping", response_model=PingResponse)
def ping_worker_config(body: WorkerConfigForm):
    existing = _load_yaml()
    api_key = "" if _uses_local_codex(body) else _resolve_secret(body, existing)
    if not _uses_local_codex(body) and not api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    if _uses_local_codex(body):
        prompt = "Reply with exactly: pong"
        argv = ["codex", "exec"]
        argv.append("--dangerously-bypass-approvals-and-sandbox")
        if body.model:
            argv.extend(["--model", body.model])
        argv.extend(["--", prompt])
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PingResponse(ok=False, preview=str(exc))
        preview = (completed.stdout or completed.stderr)[:1000]
        return PingResponse(
            ok=completed.returncode == 0 and "pong" in preview.lower(),
            status_code=completed.returncode,
            preview=preview,
        )

    try:
        if body.provider == "claudecode":
            response = requests.post(
                f"{body.base_url.rstrip('/')}/v1/messages",
                headers={
                    "authorization": f"Bearer {api_key}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": body.model,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=20,
            )
        else:
            response = requests.post(
                f"{body.base_url.rstrip('/')}/responses",
                headers={
                    "authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": body.model,
                    "input": [{"role": "user", "content": "ping"}],
                    "stream": False,
                },
                timeout=20,
            )
    except requests.RequestException as exc:
        return PingResponse(ok=False, preview=str(exc))

    return PingResponse(
        ok=response.ok,
        status_code=response.status_code,
        preview=response.text[:500],
    )


def _load_yaml() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read dispatch config: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return data


def _write_yaml(config: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to write dispatch config: {exc}") from exc


def _resolve_secret(body: WorkerConfigForm, existing: dict) -> str:
    if body.api_key and body.api_key != MASKED_SECRET:
        return body.api_key
    worker = (existing.get("workers") or [{}])[0]
    env = worker.get("env") or {}
    if body.provider == "claudecode":
        return str(env.get("ANTHROPIC_AUTH_TOKEN") or "")
    return str(env.get("OPENAI_API_KEY") or "")


def _uses_local_codex(body: WorkerConfigForm) -> bool:
    return body.provider == "codex" and body.auth_mode == "local"


def _get_model_catalog(provider: str) -> tuple[list[str], str]:
    registry = _load_model_registry()
    data = registry.get(provider, DEFAULT_MODEL_REGISTRY.get(provider, {"items": [], "default": ""}))
    items = data.get("items", [])
    default_model = data.get("default", "")
    if default_model not in items:
        default_model = items[0] if items else ""
    return items, default_model


def _load_model_registry() -> dict[str, dict[str, list[str] | str]]:
    registry_path = _registry_path()
    if not registry_path.exists():
        return {
            "codex": dict(DEFAULT_MODEL_REGISTRY["codex"]),
            "claudecode": dict(DEFAULT_MODEL_REGISTRY["claudecode"]),
        }
    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read model registry: {exc}") from exc
    if not isinstance(raw, dict):
        return {
            "codex": dict(DEFAULT_MODEL_REGISTRY["codex"]),
            "claudecode": dict(DEFAULT_MODEL_REGISTRY["claudecode"]),
        }

    result: dict[str, dict[str, list[str] | str]] = {
        "codex": {"items": [], "default": ""},
        "claudecode": {"items": [], "default": ""},
    }
    for provider in ("codex", "claudecode"):
        values = raw.get(provider)
        if isinstance(values, dict):
            items = _normalize_items(values.get("items"))
            if not items:
                items = list(DEFAULT_MODEL_REGISTRY.get(provider, {}).get("items", []))
            default_model = str(values.get("default", DEFAULT_MODEL_REGISTRY.get(provider, {}).get("default", ""))).strip()
            if default_model not in items:
                default_model = items[0] if items else ""
            result[provider] = {"items": items, "default": default_model}
    return result


def _normalize_items(raw_items: object) -> list[str]:
    if not isinstance(raw_items, list):
        return []
    normalized: list[str] = []
    for item in raw_items:
        model_name = str(item).strip()
        if not model_name or model_name in normalized:
            continue
        normalized.append(model_name)
    return normalized


def _write_model_registry(registry: dict[str, dict[str, list[str] | str]]) -> None:
    path = _registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(registry, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to write model registry: {exc}") from exc


def _build_dispatch_config(body: WorkerConfigForm, api_key: str, existing: dict | None = None) -> dict:
    existing = existing or {}
    worker_name = "claude-local" if body.provider == "claudecode" else "codex-local"
    if body.provider == "claudecode":
        env = {
            "ANTHROPIC_MODEL": body.model,
            "ANTHROPIC_BASE_URL": body.base_url.rstrip("/"),
            "ANTHROPIC_AUTH_TOKEN": api_key,
        }
    elif body.auth_mode == "local":
        env = {
            "CODEX_AUTH_MODE": "local",
        }
        if body.model:
            env["CODEX_MODEL"] = body.model
    else:
        env = {
            "CODEX_MODEL": body.model,
            "CODEX_AUTH_MODE": "api_key",
            "CODEX_BASE_URL": body.base_url.rstrip("/"),
            "OPENAI_API_KEY": api_key,
        }

    runtime = dict(existing.get("runtime") or {})
    # 每项目并发上限已迁移到项目级 projects.max_workers，重建配置时丢弃旧键。
    runtime.pop("max_project_workers", None)
    runtime.update(
        {
            "max_workers": max(1, body.max_running),
        }
    )
    runtime.setdefault("interval", 3)
    runtime.setdefault("max_running_projects", 1)
    runtime.setdefault("max_running_intakes", 2)
    runtime.setdefault("healthcheck_timeout", 20)
    runtime.setdefault("worker_healthcheck", "startup_only")
    runtime.setdefault("prompt_group", "default")

    tasks = {name: dict(value or {}) for name, value in (existing.get("tasks") or {}).items()}
    tasks.setdefault("intake", {}).setdefault("timeout", 120)
    tasks.setdefault("bootstrap", {}).update(
        {
            "timeout": tasks.get("bootstrap", {}).get("timeout", 300),
            "conclude_timeout": tasks.get("bootstrap", {}).get("conclude_timeout", 90),
        }
    )
    tasks.setdefault("reason", {}).update(
        {
            "timeout": tasks.get("reason", {}).get("timeout", 300),
            "max_intents": tasks.get("reason", {}).get("max_intents", 2),
        }
    )
    explore = tasks.setdefault("explore", {})
    explore.setdefault("timeout", 300)
    explore.setdefault("conclude_timeout", 90)
    explore.setdefault("checkpoint_timeout", 90)
    explore.setdefault("max_session_attempts", 3)
    explore.setdefault("retry_delay", 15)
    explore.setdefault("max_no_progress_slices", 2)

    config = dict(existing)
    config["server"] = existing.get("server") or "http://127.0.0.1:8000"
    config["runtime"] = runtime
    config["tasks"] = tasks
    config["container"] = dict(
        existing.get("container")
        or {
            "image": "cairn-worker-container:latest",
            "network_mode": "host",
            "completed_action": "remove",
            "workspace_mount": "/cairn/workspace",
            "manage_docker": False,
            "docker_binary": "docker",
            "reap_orphans": True,
            "confine_workdir": True,
        }
    )
    config["common_env"] = dict(
        existing.get("common_env")
        or {
            "TSEC_BASE_URL": "http://127.0.0.1:8000/api",
            "TSEC_AGENT_TOKEN": "local-agent-token",
        }
    )
    existing_workers = existing.get("workers")
    workers = [dict(worker) for worker in existing_workers] if isinstance(existing_workers, list) else []
    current = workers[0] if workers else {}
    current_env = current.get("env") if isinstance(current.get("env"), dict) else {}
    provider_keys = {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "CODEX_MODEL",
        "CODEX_AUTH_MODE",
        "CODEX_BASE_URL",
        "OPENAI_API_KEY",
    }
    # Web 表单只编辑第一个 Worker；保留其他 Worker 和自定义环境变量，
    # 避免一次 Save 静默破坏手写的多 Worker 调度配置。
    custom_env = {key: value for key, value in current_env.items() if key not in provider_keys}
    updated_worker = {
        **current,
        "name": worker_name,
        "type": body.provider,
        "task_types": ["intake", "bootstrap", "reason", "explore"],
        "max_running": body.max_running,
        "priority": current.get("priority", 0),
        "env": {**custom_env, **env},
    }
    config["workers"] = [updated_worker, *workers[1:]]
    return config
