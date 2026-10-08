from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from importlib import resources
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskType = Literal["reason", "explore", "bootstrap", "vulnerability_analysis", "verify"]
WorkerType = Literal["claudecode", "codex", "gemini", "opencode", "pi", "mock"]
CompletedAction = Literal["remove", "stop"]
WorkerHealthcheckMode = Literal["startup_and_task", "startup_only", "disabled"]
ExecutionMode = Literal["container", "local"]
LocalCompletedAction = Literal["keep", "remove"]
AuthControlPlaneMode = Literal["legacy", "dual_write", "enforced"]

RESERVED_RUNTIME_ENV_KEYS = frozenset(
    {
        "CAIRN_PROJECT_ID",
        "CAIRN_INTENT_ID",
        "CAIRN_RUN_ID",
        "CAIRN_WORKER",
        "CAIRN_PHASE",
    }
)

# Dispatcher process secrets. A target credential mapping must not name these,
# even when an operator writes the env var into dispatch config.
DENIED_TARGET_ENVS = frozenset(
    {
        "CAIRN_ADMIN_TOKEN",
        "CAIRN_SAFETY_TOKEN",
        "CAIRN_AUTH_HELPER_TOKEN",
    }
)


def is_denied_target_env(name: str) -> bool:
    key = name.strip().upper()
    if not key:
        return True
    if key in DENIED_TARGET_ENVS:
        return True
    return key.startswith("CAIRN_SAFETY_")

WORKER_ENV_KEYS: dict[WorkerType, tuple[str, ...]] = {
    "claudecode": (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
    ),
    "codex": (
        "CODEX_MODEL",
        "CODEX_BASE_URL",
        "OPENAI_API_KEY",
    ),
    "pi": (
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
    "gemini": (),
    "opencode": (
        "OPENCODE_MODEL",
        "OPENCODE_BASE_URL",
        "OPENCODE_API_KEY",
    ),
    "mock": (),
}

DEFAULT_PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "reason.md": ("{graph_yaml}", "{fact_ids}", "{open_intents}", "{max_intents}"),
    "explore.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "explore_conclude.md": ("{graph_yaml}", "{intent_id}", "{intent_description}", "{safety_decision_context}"),
    "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
    "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}", "{safety_decision_context}"),
    "verify.md": ("{graph_yaml}", "{intent_id}", "{intent_description}", "{poc_brief}"),
    "verify_conclude.md": ("{graph_yaml}", "{intent_id}", "{intent_description}", "{poc_brief}"),
}

PROMPT_REQUIRED_TOKENS_BY_GROUP: dict[str, dict[str, tuple[str, ...]]] = {
    "mock": {
        "reason.md": ("{fact_ids}", "{open_intents}", "{max_intents}"),
        "explore.md": ("{intent_id}",),
        "explore_conclude.md": ("{intent_id}",),
        "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
        "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
    }
}

MOCK_ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    "healthcheck": frozenset({"ok", "fail"}),
    "reason": frozenset({"complete", "intent", "noop", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_execute": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap": frozenset({"complete", "fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
}

MOCK_DEFAULT_BEHAVIOR: dict[str, dict[str, Any]] = {
    "healthcheck": {
        "delay": [0.05, 0.15],
        "outcomes": {"ok": "1.0", "fail": "0.0"},
    },
    "reason": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "0.0",
            "intent": "1.0",
            "noop": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_execute": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "1.0",
            "fact": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
}

MOCK_ALLOWED_ENV_KEYS = frozenset(
    {f"MOCK_{phase.upper()}" for phase in MOCK_ALLOWED_OUTCOMES}
)


class ReasonTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    max_intents: int = Field(gt=0, default=3)


class ExploreTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class BootstrapTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class VerifyTaskConfig(BaseModel):
    timeout: int = Field(default=120, gt=0)
    conclude_timeout: int = Field(default=30, gt=0)
    max_rounds: int = Field(default=3, ge=1, le=8)
    require_fire_approval: bool = True
    force_harness: bool = True
    allow_model_instantiate: bool = False
    proxy_url: str | None = None


class TargetCredentialConfig(BaseModel):
    """Operator binding for one verify bearer token.

    Project Origin may name ``id`` (or ``secret:<id>``). The environment variable,
    destination, and allowlist come from this object, not from Origin.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    env: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    allowlist: list[str] = Field(min_length=1)

    @field_validator("id", "env", "base_url")
    @classmethod
    def strip_required(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("env")
    @classmethod
    def reject_internal_env(cls, value: str) -> str:
        if is_denied_target_env(value):
            raise ValueError("dispatcher-internal secrets cannot be target credentials")
        return value

    @field_validator("base_url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an http(s) URL")
        return value

    @field_validator("allowlist")
    @classmethod
    def clean_allowlist(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not cleaned:
            raise ValueError("allowlist must not be empty")
        return cleaned


class TasksConfig(BaseModel):
    bootstrap: BootstrapTaskConfig
    reason: ReasonTaskConfig
    explore: ExploreTaskConfig
    verify: VerifyTaskConfig = Field(default_factory=VerifyTaskConfig)


class ContainerProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str | None = None
    network_mode: str | None = None


class ResolvedContainerProfile(BaseModel):
    image: str
    network_mode: str


class AuthVerifyConfig(BaseModel):
    url: str
    expect_status: int = 200
    selector: str | None = None
    http_url: str | None = None
    http_expect_status: int = 200


class AuthTargetConfig(BaseModel):
    name: str
    base_url: str
    login_url: str
    role: str
    request_reason: str = "authentication_required"
    indexed_db: bool = True
    verify: AuthVerifyConfig


class AuthInterventionConfig(BaseModel):
    enabled: bool = True
    request_ttl: int = Field(default=1800, ge=0)
    claim_ttl: int = Field(default=300, ge=0)
    allow_roles: list[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    store_root: str
    worker_mount_root: str = "/run/cairn-auth"
    login_timeout: int = Field(default=600, gt=0)
    verify_timeout: int = Field(default=30, gt=0)
    helper_token_env: str = "CAIRN_AUTH_HELPER_TOKEN"
    helper_actor_id: str = "helper"
    helper_scopes: list[str] = Field(default_factory=lambda: ["helper.event.submit", "helper.request.read"])
    helper_project_allowlist: list[str] = Field(default_factory=list)
    targets: list[AuthTargetConfig] = Field(default_factory=list)
    intervention: AuthInterventionConfig = Field(default_factory=AuthInterventionConfig)

    def target(self, name: str) -> AuthTargetConfig:
        for candidate in self.targets:
            if candidate.name == name:
                return candidate
        raise KeyError(f"auth target not found: {name}")


class ContainerConfig(BaseModel):
    image: str
    network_mode: str
    completed_action: CompletedAction
    cap_add: list[str] = Field(default_factory=list)
    verify: ContainerProfileConfig | None = None
    codebase_mount_path: str = "/codebase"

    def resolve_profile(self, name: str) -> ResolvedContainerProfile:
        override = self.verify if name == "verify" else None
        return ResolvedContainerProfile(
            image=(override.image if override and override.image else self.image),
            network_mode=(override.network_mode if override and override.network_mode else self.network_mode),
        )


class LocalConfig(BaseModel):
    workspace_root: str | None = None
    completed_action: LocalCompletedAction = "keep"


class RuntimeConfig(BaseModel):
    max_workers: int = Field(gt=0)
    max_running_projects: int = Field(gt=0)
    max_project_workers: int = Field(gt=0)
    interval: int = Field(gt=0)
    healthcheck_timeout: int = Field(gt=0)
    worker_healthcheck: WorkerHealthcheckMode = "startup_only"
    execution: ExecutionMode = "container"
    prompt_group: str = Field(min_length=1)
    stale_retry_threshold: int = Field(default=3, ge=1)
    dead_retry_threshold: int = Field(default=10, ge=1)
    project_timeout: int | None = Field(default=None, gt=0)
    server_lease_timeout: int | None = Field(default=None, gt=0)
    heartbeat_failure_grace: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_heartbeat_grace(self) -> "RuntimeConfig":
        if self.heartbeat_failure_grace is not None and self.heartbeat_failure_grace <= self.interval:
            raise ValueError("heartbeat_failure_grace must be greater than interval")
        effective_grace = self.heartbeat_failure_grace or self.interval * 2
        if self.server_lease_timeout is not None and self.server_lease_timeout <= effective_grace:
            raise ValueError("server_lease_timeout must be greater than heartbeat_failure_grace")
        return self


class ResourceBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_bulk_concurrency: int = Field(default=2, ge=1, le=2)
    max_unattended_bulk_seconds: int = Field(default=600, ge=30, le=600)
    auth_concurrency: int = Field(default=1, ge=1, le=1)
    auth_attempts_per_minute: int = Field(default=10, ge=1, le=10)
    auth_attempts_per_batch: int = Field(default=30, ge=1, le=30)


class SafetyConfig(BaseModel):
    """Optional Pi preflight. Other worker types keep running when this block is absent."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    endpoint: str = Field(min_length=1)
    token_env: str = Field(default="CAIRN_SAFETY_TOKEN", min_length=1)
    request_timeout_ms: int = Field(default=2000, ge=100, le=10000)
    max_payload_bytes: int = Field(default=65536, ge=4096, le=1048576)
    resource_budget: ResourceBudgetConfig = Field(default_factory=ResourceBudgetConfig)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: WorkerType
    task_types: list[TaskType]
    max_running: int = Field(gt=0)
    priority: int = Field(ge=0)
    env: dict[str, str] = Field(default_factory=dict)
    difficulties: list[str] | None = None
    model: str | None = None
    capabilities: list[Literal["static_fs", "live_http", "browser"]] | None = None

    def has_capabilities(self, required: list[str]) -> bool:
        if self.capabilities is None:
            return True
        owned = set(self.capabilities)
        return all(item in owned for item in required)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("model must not be empty")
        return text

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[TaskType]) -> list[TaskType]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @model_validator(mode="after")
    def validate_env(self) -> "WorkerConfig":
        # Required LLM env keys (base_url / key / model) are enforced per execution mode by
        # DispatchConfig: container mode needs them, local mode reuses the host CLI config.
        # The checks below are mode-independent and always apply.
        if self.type == "pi":
            _validate_optional_positive_int_env(self.name, self.env, "PI_MODEL_CONTEXT_WINDOW")
        if self.type == "mock":
            resolve_mock_behavior(self.name, self.env)
        return self


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str
    server_token: str | None = None
    auth_control_plane_mode: AuthControlPlaneMode = "legacy"
    runtime: RuntimeConfig
    tasks: TasksConfig
    container: ContainerConfig | None = None
    local: LocalConfig | None = None
    safety: SafetyConfig | None = None
    common_env: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig | None = None
    target_credentials: list[TargetCredentialConfig] = Field(default_factory=list)
    workers: list[WorkerConfig]

    @model_validator(mode="before")
    @classmethod
    def merge_common_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        common_env = data.get("common_env")
        if common_env is None:
            common_env = {}
        workers = data.get("workers")
        if not isinstance(common_env, dict) or not isinstance(workers, list):
            return data

        reserved = {key for key in common_env if _is_reserved_safety_env(key)}
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            worker_env = worker.get("env")
            if isinstance(worker_env, dict):
                reserved.update(key for key in worker_env if _is_reserved_safety_env(key))
        if reserved:
            raise ValueError(f"reserved safety env keys are managed by Cairn: {', '.join(sorted(reserved))}")

        merged = dict(data)
        merged_workers: list[Any] = []
        for worker in workers:
            if not isinstance(worker, dict):
                merged_workers.append(worker)
                continue
            worker_env = worker.get("env")
            if worker_env is None:
                worker_env = {}
            if not isinstance(worker_env, dict):
                merged_workers.append(worker)
                continue
            worker_copy = dict(worker)
            worker_copy["env"] = {**common_env, **worker_env}
            merged_workers.append(worker_copy)
        merged["workers"] = merged_workers
        return merged

    @model_validator(mode="after")
    def validate_workers(self) -> "DispatchConfig":
        names = [worker.name for worker in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("worker names must be unique")
        if not self.workers:
            raise ValueError("workers must not be empty")
        if self.runtime.max_project_workers > self.runtime.max_workers:
            raise ValueError("max_project_workers cannot exceed max_workers")
        return self

    @model_validator(mode="after")
    def validate_execution_mode(self) -> "DispatchConfig":
        if self.runtime.execution == "container":
            if self.container is None:
                raise ValueError("container config is required when runtime.execution is container")
            for worker in self.workers:
                required = WORKER_ENV_KEYS[worker.type]
                missing = [key for key in required if not worker.env.get(key)]
                if missing:
                    raise ValueError(f"worker {worker.name} missing env keys: {', '.join(missing)}")
        else:  # local: workers reuse the host CLI config, so no LLM env keys are required
            if self.local is None:
                self.local = LocalConfig()
        return self

    @model_validator(mode="after")
    def validate_target_credentials(self) -> "DispatchConfig":
        ids = [item.id for item in self.target_credentials]
        if len(ids) != len(set(ids)):
            raise ValueError("target_credentials ids must be unique")
        denied: set[str] = set()
        if self.safety is not None and self.safety.token_env.strip():
            denied.add(self.safety.token_env.strip().upper())
        if self.auth is not None and self.auth.helper_token_env.strip():
            denied.add(self.auth.helper_token_env.strip().upper())
        for item in self.target_credentials:
            if item.env.upper() in denied:
                raise ValueError(f"target credential {item.id} env is a dispatcher-internal secret")
        return self

    @model_validator(mode="after")
    def validate_auth_targets(self) -> "DispatchConfig":
        if self.auth is None:
            return self
        names = [target.name for target in self.auth.targets]
        if len(set(names)) != len(names):
            raise ValueError("auth target names must be unique")
        return self

    def auth_deployment_snapshot(self) -> dict[str, Any]:
        """Deployment payload for the server. Callers must not log raw tokens."""
        snapshot: dict[str, Any] = {"dispatcher_token": self.server_token}
        if self.auth is None:
            return snapshot
        snapshot.update(
            {
                "helper_token": os.environ.get(self.auth.helper_token_env),
                "helper_actor_id": self.auth.helper_actor_id,
                "helper_scopes": list(self.auth.helper_scopes),
                "helper_project_allowlist": list(self.auth.helper_project_allowlist),
                "targets": {target.name: target.login_url for target in self.auth.targets},
                "target_roles": {target.name: target.role for target in self.auth.targets},
                "target_reasons": {target.name: target.request_reason for target in self.auth.targets},
            }
        )
        return snapshot

    @classmethod
    def load(cls, path: Path) -> "DispatchConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        validate_prompt_resources(config.runtime.prompt_group)
        return config


def _validate_optional_positive_int_env(worker_name: str, env: dict[str, str], key: str) -> None:
    value = env.get(key)
    if value is None or not value.strip():
        return
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"worker {worker_name} env {key} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"worker {worker_name} env {key} must be greater than 0")


def _is_reserved_safety_env(key: object) -> bool:
    return isinstance(key, str) and (key.startswith("CAIRN_SAFETY_") or key in RESERVED_RUNTIME_ENV_KEYS)


def resolve_safety_token(config: SafetyConfig) -> str:
    value = os.environ.get(config.token_env, "").strip()
    if not value:
        raise ValueError(f"missing safety token environment variable: {config.token_env}")
    return value


def validate_prompt_resources(prompt_group: str) -> None:
    prompts_dir = resources.files("cairn.dispatcher.prompts")
    group_dir = prompts_dir.joinpath(prompt_group)
    if not group_dir.is_dir():
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = PROMPT_REQUIRED_TOKENS_BY_GROUP.get(prompt_group, DEFAULT_PROMPT_REQUIRED_TOKENS)
    for name, tokens in required_tokens.items():
        try:
            content = group_dir.joinpath(name).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}")


def resolve_mock_behavior(worker_name: str, env: dict[str, str]) -> dict[str, dict[str, Any]]:
    unknown = sorted(key for key in env if key.startswith("MOCK_") and key not in MOCK_ALLOWED_ENV_KEYS)
    if unknown:
        raise ValueError(f"worker {worker_name} has unsupported mock env keys: {', '.join(unknown)}")

    behavior: dict[str, dict[str, Any]] = {}
    for phase, allowed_outcomes in MOCK_ALLOWED_OUTCOMES.items():
        prefix = _mock_env_prefix(phase)
        payload = _parse_mock_phase_payload(worker_name, env, prefix, MOCK_DEFAULT_BEHAVIOR[phase])
        min_delay, max_delay = _parse_mock_delay_range(worker_name, prefix, payload.get("delay"))
        if max_delay < min_delay:
            raise ValueError(f"worker {worker_name} {prefix}.delay[1] must be greater than or equal to delay[0]")
        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_outcomes, dict):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes must be an object")
        unknown_outcomes = sorted(set(raw_outcomes) - allowed_outcomes)
        if unknown_outcomes:
            raise ValueError(f"worker {worker_name} {prefix}.outcomes has unsupported keys: {', '.join(unknown_outcomes)}")
        outcomes: dict[str, float] = {}
        total = Decimal("0")
        for outcome in sorted(allowed_outcomes):
            weight = _parse_mock_probability(
                worker_name,
                prefix,
                raw_outcomes,
                outcome,
            )
            outcomes[outcome] = float(weight)
            total += weight
        if total != Decimal("1"):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes probabilities must sum to 1.0, got {total}")
        behavior[phase] = {
            "delay": {"min": min_delay, "max": max_delay},
            "outcomes": outcomes,
        }
        rules = payload.get("rules")
        if rules is not None:
            if not isinstance(rules, list):
                raise ValueError(f"worker {worker_name} {prefix}.rules must be an array")
            normalized_rules: list[dict[str, Any]] = []
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(f"worker {worker_name} {prefix}.rules[{index}] must be an object")
                force = rule.get("force")
                if not isinstance(force, str) or force not in allowed_outcomes:
                    raise ValueError(
                        f"worker {worker_name} {prefix}.rules[{index}].force must be one of: {', '.join(sorted(allowed_outcomes))}"
                    )
                entry: dict[str, Any] = {"force": force}
                if "fact_ids_gte" in rule:
                    value = rule["fact_ids_gte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_gte must be a non-negative integer")
                    entry["fact_ids_gte"] = value
                if "fact_ids_lte" in rule:
                    value = rule["fact_ids_lte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_lte must be a non-negative integer")
                    entry["fact_ids_lte"] = value
                if "open_intents_empty" in rule:
                    value = rule["open_intents_empty"]
                    if not isinstance(value, bool):
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].open_intents_empty must be boolean")
                    entry["open_intents_empty"] = value
                normalized_rules.append(entry)
            behavior[phase]["rules"] = normalized_rules
    return behavior


def _mock_env_prefix(phase: str) -> str:
    return f"MOCK_{phase.upper()}"


def _parse_mock_phase_payload(worker_name: str, env: dict[str, str], key: str, default: dict[str, Any]) -> dict[str, Any]:
    raw = env.get(key)
    if raw is None:
        return json.loads(json.dumps(default))
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker {worker_name} {key} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"worker {worker_name} {key} must be a JSON object")
    return value


def _parse_mock_delay_range(worker_name: str, key: str, value: Any) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"worker {worker_name} {key}.delay must be a two-element number array")
    min_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[0]", value[0])
    max_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[1]", value[1])
    return min_delay, max_delay


def _coerce_mock_seconds(worker_name: str, key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"worker {worker_name} {key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"worker {worker_name} {key} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"worker {worker_name} {key} must be non-negative")
    return parsed


def _parse_mock_probability(worker_name: str, phase_key: str, outcomes: dict[str, Any], outcome: str) -> Decimal:
    raw = outcomes.get(outcome, MOCK_DEFAULT_BEHAVIOR[phase_key.removeprefix("MOCK_").lower()]["outcomes"][outcome])
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be a decimal probability") from exc
    if value < 0 or value > 1:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be between 0 and 1")
    return value
