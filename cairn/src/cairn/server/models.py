from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.dispatcher.config import ResourceBudgetConfig

FactType = Literal["source", "sink", "dataflow", "constraint", "gadget", "reachability", "verification"]
ConfidenceLevel = Literal["hypothesized", "static-confirmed", "reachable-confirmed", "poc-confirmed", "refuted"]
BaseKnowledgeKind = Literal["architecture", "auth", "routing", "trust_boundary", "convention"]
BaseKnowledgeConfidence = Literal["assumed", "code-confirmed", "live-confirmed"]
RoutingVia = Literal["direct", "gateway_rewrite", "spa_route"]


AuditEventType = Literal[
    "ACTION_DECISION",
    "ACTION_RESULT",
    "ASSISTANT_MESSAGE",
    "AGENT_END",
    "AUDIT_BACKFILL",
]
AuditDecision = Literal["allow", "block", "resource_pause"]


class AuditEventCreate(BaseModel):
    schema_version: Literal[1]
    event_id: str = Field(min_length=1)
    action_id: str | None = None
    run_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    intent_id: str | None = None
    worker: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    event_type: AuditEventType
    tool_name: str | None = None
    decision: AuditDecision | None = None
    rule_id: str | None = None
    reason: str | None = None
    payload: dict[str, Any]


class AuditEvent(BaseModel):
    event_id: str
    action_id: str | None = None
    run_id: str
    project_id: str
    intent_id: str | None = None
    worker: str
    phase: str
    event_type: AuditEventType
    tool_name: str | None = None
    decision: AuditDecision | None = None
    rule_id: str | None = None
    reason: str | None = None
    payload: dict[str, Any]
    payload_sha256: str
    truncated: bool
    created_at: str


class AuditEventPage(BaseModel):
    items: list[AuditEvent]
    next: str | None = None


class SafetyPreflightRequest(BaseModel):
    schema_version: Literal[1]
    event_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    intent_id: str | None = None
    worker: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    input: dict[str, Any]
    cwd: str = Field(min_length=1)
    resource_budget: ResourceBudgetConfig | None = None


class SafetyPreflightResponse(BaseModel):
    event_id: str
    action_id: str
    decision: AuditDecision
    rule_id: str | None = None
    reason: str
    target: str | None = None
    auth_attempt_count: int = Field(default=0, ge=0)


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)
    auth_claim_ttl: int = Field(default=300, ge=0)
    auth_request_ttl: int = Field(default=1800, ge=0)
    auth_control_plane_mode: Literal["legacy", "dual_write", "enforced"] = "legacy"


class Fact(BaseModel):
    id: str
    description: str
    type: FactType | None = None
    confidence: ConfidenceLevel | None = None
    locations: list[str] | None = None
    code_version: str | None = None
    evidence: str | None = None
    verifies: str | None = None
    intent_id: str | None = None
    batch_id: str | None = None
    oracle_draft: str | None = None
    payload_draft: str | None = None
    effective_confidence: ConfidenceLevel | None = None
    stale: bool = False


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    concluded_as: Literal["success", "dead", "stale", "blocked"] | None = None
    retry_count: int = 0
    task_kind: Literal["explore", "verify"] | None = None
    poc_brief: dict | None = None
    fire_status: Literal["pending", "approved", "denied", "fired"] | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    started_at: str | None = None
    difficulty: str | None = None
    backend: Literal["docker", "local"] | None = None
    project_root: str | None = None
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class InitFile(BaseModel):
    id: str
    path: str
    content: str
    encoding: str = "utf-8"


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    init_files: list[InitFile] = []
    base_knowledge: dict | None = None


class CreateInitFileInline(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        from cairn.dispatcher.runtime.workspace_files import InitFilePathError, relative_init_path

        try:
            relative_init_path(value)
        except InitFilePathError as exc:
            raise ValueError(str(exc)) from exc
        return value.strip()


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    difficulty: str | None = None
    hints: list[CreateHintInline] | None = None
    init_files: list[CreateInitFileInline] | None = None
    backend: Literal["docker", "local"] | None = None
    project_root: str | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None
    task_kind: Literal["explore", "verify"] | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class MarkIntentFailedRequest(BaseModel):
    worker: str
    stale_retry_threshold: int = Field(default=3, ge=1)
    dead_retry_threshold: int = Field(default=10, ge=1)

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str | None = None
    observations: list[Observation] | None = None
    base_knowledge_patches: list[BaseKnowledgePatchEmit] | None = None

    @field_validator("worker")
    @classmethod
    def validate_worker(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @model_validator(mode="after")
    def validate_payload(self) -> "ConcludeRequest":
        if self.description is not None and self.observations is not None:
            raise ValueError("description and observations cannot coexist")
        if self.description is not None and not self.description.strip():
            raise ValueError("description must not be empty")
        if self.observations is not None and len(self.observations) == 0:
            raise ValueError("observations must not be empty")
        if self.description is None and not self.observations:
            raise ValueError("either description or observations is required")
        return self


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent
    facts: list[Fact] = []


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


class RuntimeEvent(BaseModel):
    id: int
    project_id: str | None = None
    event_type: str
    phase: str | None = None
    status: str
    message: str
    worker: str | None = None
    intent_id: str | None = None
    payload: dict[str, Any] | None = None
    created_at: str


class CreateRuntimeEventRequest(BaseModel):
    event_type: str
    phase: str | None = None
    status: str = "info"
    message: str
    worker: str | None = None
    intent_id: str | None = None
    payload: dict[str, Any] | None = None

    @field_validator("event_type", "status", "message")
    @classmethod
    def validate_runtime_event_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class HttpMessage(BaseModel):
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class HttpResponseMessage(HttpMessage):
    status: int | None = Field(default=None, ge=100, le=599)


class CreateHttpRecordRequest(BaseModel):
    intent_id: str | None = None
    worker: str | None = None
    method: str
    url: str
    request: HttpMessage = Field(default_factory=HttpMessage)
    response: HttpResponseMessage = Field(default_factory=HttpResponseMessage)
    significance: str

    @field_validator("method", "url", "significance")
    @classmethod
    def validate_http_record_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("method")
    @classmethod
    def normalize_http_method(cls, value: str) -> str:
        return value.upper()


class HttpRecord(CreateHttpRecordRequest):
    id: str
    project_id: str
    created_at: str


class DispatchConfigDocument(BaseModel):
    path: str
    yaml: str
    redacted: bool
    updated_at: str | None = None
    restart_required: bool = False


class UpdateDispatchConfigRequest(BaseModel):
    yaml: str

    @field_validator("yaml")
    @classmethod
    def validate_yaml_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("yaml must not be empty")
        return value


class CtfMode(str, Enum):
    MANUAL = "manual"
    CTF = "ctf"


class CtfConfig(BaseModel):
    mode: CtfMode = CtfMode.MANUAL
    adapter: str = "ctfd"
    base_url: str = ""
    token: str = ""
    team_name: str = ""
    flag_regex: str = "flag\\{[^}]+\\}"
    auto_submit: bool = True
    max_concurrent: int = Field(default=2, ge=1, le=32)
    poll_interval: int = Field(default=10, ge=3, le=3600)
    env_poll_interval: int = Field(default=5, ge=1, le=300)
    env_timeout: int = Field(default=180, ge=10, le=3600)
    submission_max_retries: int = Field(default=5, ge=1, le=50)
    rate_limit_backoff: int = Field(default=30, ge=1, le=3600)
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: str = ""
    last_model_error: str | None = None
    model_health_at: str | None = None
    budget_easy: int = Field(default=12, ge=1, le=10000)
    budget_medium: int = Field(default=25, ge=1, le=10000)
    budget_hard: int = Field(default=40, ge=1, le=10000)
    last_sync_at: str | None = None
    bridge_heartbeat_at: str | None = None
    bridge_error: str | None = None
    sync_requested: bool = False


class CtfConfigUpdate(BaseModel):
    adapter: str | None = None
    base_url: str | None = None
    token: str | None = None
    team_name: str | None = None
    flag_regex: str | None = None
    auto_submit: bool | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=32)
    poll_interval: int | None = Field(default=None, ge=3, le=3600)
    env_poll_interval: int | None = Field(default=None, ge=1, le=300)
    env_timeout: int | None = Field(default=None, ge=10, le=3600)
    submission_max_retries: int | None = Field(default=None, ge=1, le=50)
    rate_limit_backoff: int | None = Field(default=None, ge=1, le=3600)
    model_base_url: str | None = None
    model_name: str | None = None
    model_api_key: str | None = None
    budget_easy: int | None = Field(default=None, ge=1, le=10000)
    budget_medium: int | None = Field(default=None, ge=1, le=10000)
    budget_hard: int | None = Field(default=None, ge=1, le=10000)

    @field_validator("adapter", "base_url", "team_name", "flag_regex", "model_base_url", "model_name")
    @classmethod
    def validate_text_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


class CtfModeRequest(BaseModel):
    mode: CtfMode


class CtfChallenge(BaseModel):
    id: int
    external_id: str
    title: str
    category: str = ""
    points: int = 0
    description: str = ""
    target: str = ""
    attachments: list[str] = []
    hints: list[str] = []
    status: str = "queued"
    project_id: str | None = None
    last_flag: str | None = None
    attempt_count: int = 0
    needs_refresh: bool = False
    created_at: str
    updated_at: str


class CtfSubmitRequest(BaseModel):
    challenge_id: str
    flag: str

    @field_validator("challenge_id", "flag")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CtfHeartbeatRequest(BaseModel):
    error: str | None = None
    model_ok: bool | None = None
    model_error: str | None = None


class CtfTestResult(BaseModel):
    ok: bool
    detail: str


class Observation(BaseModel):
    type: FactType | None = None
    description: str
    locations: list[str] | None = None
    evidence: str | None = None
    oracle_draft: str | None = None
    payload_draft: str | None = None
    verifies: str | None = None
    confidence: ConfidenceLevel | None = None
    why_failed: dict | None = None


class PoCBriefEntry(BaseModel):
    endpoint: str
    precondition: str = "none"


class PoCBriefPayloadRecipe(BaseModel):
    gadget: str | None = None
    shape: str = ""


class PoCBriefSuccessSignature(BaseModel):
    kind: str = "response_match"
    check: str = ""


class PoCBrief(BaseModel):
    chain: list[str] = Field(default_factory=list)
    entry: PoCBriefEntry
    dataflow: str = ""
    payload_recipe: PoCBriefPayloadRecipe = Field(default_factory=PoCBriefPayloadRecipe)
    success_signature: PoCBriefSuccessSignature = Field(default_factory=PoCBriefSuccessSignature)
    constraints_to_bypass: list[str] = Field(default_factory=list)


class BaseKnowledgePatchEmit(BaseModel):
    entry_id: str
    statement: str | None = None
    evidence: list[str] | None = None
    confidence: BaseKnowledgeConfidence | None = None


class BaseKnowledgeEntry(BaseModel):
    id: str
    kind: BaseKnowledgeKind
    statement: str
    evidence: list[str] = Field(default_factory=list)
    confidence: BaseKnowledgeConfidence = "assumed"
    revised_by: str | None = None


class RoutingMapEntry(BaseModel):
    src: str
    live: str
    via: RoutingVia = "direct"
    confidence: BaseKnowledgeConfidence = "assumed"


class BaseKnowledgeAudit(BaseModel):
    entry_id: str
    revised_by: str | None = None
    actor: str
    action: str
    at: str


class BaseKnowledge(BaseModel):
    version: int = 0
    entries: list[BaseKnowledgeEntry] = Field(default_factory=list)
    routing_map: list[RoutingMapEntry] = Field(default_factory=list)
    audit: list[BaseKnowledgeAudit] = Field(default_factory=list)


class PutBaseKnowledgeRequest(BaseModel):
    entries: list[BaseKnowledgeEntry] = Field(default_factory=list)
    routing_map: list[RoutingMapEntry] = Field(default_factory=list)
    expected_version: int | None = None
    actor: str = "worker"


class PatchBaseKnowledgeEntryRequest(BaseModel):
    statement: str | None = None
    evidence: list[str] | None = None
    confidence: BaseKnowledgeConfidence | None = None
    revised_by: str
    actor: str = "worker"
    expected_version: int | None = None


class FireApprovalRequest(BaseModel):
    action: Literal["approve", "deny"]
    actor: str = "human"


class KillVerifyRequest(BaseModel):
    actor: str = "human"
    reason: str = "kill-switch"


class VerifyControlState(BaseModel):
    project_id: str
    kill_requested: bool = False
    kill_requested_at: str | None = None
    kill_actor: str | None = None
    kill_reason: str | None = None


class ProxyTrafficEntry(BaseModel):
    id: str
    project_id: str
    intent_id: str | None = None
    request: str
    response: str | None = None
    baseline: str | None = None
    created_at: str
    status: Literal["recorded", "approved", "denied", "blocked"] = "recorded"


class RecordProxyTrafficRequest(BaseModel):
    intent_id: str | None = None
    request: str
    response: str | None = None
    baseline: str | None = None
    status: Literal["recorded", "approved", "denied", "blocked"] = "recorded"


class EngineOverride(BaseModel):
    path: str
    launcher: Literal["direct", "cmd", "powershell"] = "direct"


class EngineInfo(BaseModel):
    type: str
    binary: str
    launchable: bool
    path: str | None = None
    version: str | None = None
    source: str | None = None
    override: EngineOverride | None = None


class ToolInfo(BaseModel):
    name: str
    launchable: bool
    version: str | None = None
    path: str | None = None


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True


class SkillContent(BaseModel):
    name: str
    content: str


class SkillCreate(BaseModel):
    name: str
    content: str


class SkillEnable(BaseModel):
    enabled: bool


AuthRequestStatus = Literal[
    "pending",
    "claimed",
    "waiting_user",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "expired",
]

ACTIVE_AUTH_REQUEST_STATUSES = (
    "pending",
    "claimed",
    "waiting_user",
    "verifying",
)


class AuthRequest(BaseModel):
    id: str
    project_id: str
    source_fact_ids: list[str]
    auth_ref: str
    role: str
    login_url: str | None = None
    reason: str
    status: AuthRequestStatus
    claimed_by: str | None = None
    created_at: str
    claimed_at: str | None = None
    completed_at: str | None = None
    failure_reason: str | None = None
    helper_actor_id: str | None = None
    expires_at: str | None = None
    expiry_generation: int = 1


class CreateAuthRequest(BaseModel):
    source_fact_ids: list[str] = Field(min_length=1)
    auth_ref: str
    role: str
    login_url: str | None = None
    reason: str

    @field_validator("auth_ref", "role", "reason")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("source_fact_ids")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ClaimAuthRequest(BaseModel):
    helper_id: str

    @field_validator("helper_id")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class FailAuthRequest(BaseModel):
    failure_reason: str | None = None


class CreateAuthRequestInternal(BaseModel):
    """Dispatcher-only request creation payload.

    The Dispatcher supplies config-derived role/reason data; callers using the
    public route continue to use :class:`CreateAuthRequest` during migration.
    """

    model_config = {"extra": "forbid"}

    project_id: str
    source_fact_ids: list[str] = Field(min_length=1)
    auth_ref: str

    @field_validator("project_id", "auth_ref")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("source_fact_ids")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("fact ids must not be empty")
        return cleaned


class AuthEventClaimRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dispatcher_id: str = "dispatcher"

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


AuthEventOperation = Literal[
    "bind_actor",
    "mark_waiting_user",
    "begin_verification",
    "mark_failed",
    "reject",
]
AuthEventOutcome = Literal[
    "verified",
    "verification_failed",
    "login_failed",
    "invalid_transition",
    "not_request_owner",
    "request_mismatch",
    "retry_exhausted",
    "expired",
    "store_unavailable",
    "capture_mismatch",
    "unknown_target",
]


class AuthEventApplyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    event_id: str
    dispatcher_id: str = "dispatcher"
    operation: AuthEventOperation
    outcome_code: AuthEventOutcome | None = None

    @field_validator("event_id", "dispatcher_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class AuthEventReapRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dispatcher_id: str | None = None


AuthEventKind = Literal[
    "launch_requested",
    "browser_opened",
    "login_succeeded",
    "login_failed",
]
AuthEventState = Literal["queued", "claimed", "retryable", "applied", "rejected"]


class CreateAuthEvent(BaseModel):
    """The intentionally closed event ingress contract used by Helpers/CLI."""

    model_config = {"extra": "forbid"}

    project_id: str
    request_id: str
    auth_ref: str
    kind: AuthEventKind
    idempotency_key: UUID
    occurred_at: str
    capture_generation: int | None = Field(default=None, ge=0)

    @field_validator("project_id", "request_id", "auth_ref")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an ISO-8601 timestamp") from exc
        return text

    @model_validator(mode="after")
    def validate_capture_generation(self) -> "CreateAuthEvent":
        if self.kind == "login_succeeded" and self.capture_generation is None:
            raise ValueError("capture_generation is required for login_succeeded")
        if self.kind != "login_succeeded" and self.capture_generation is not None:
            raise ValueError("capture_generation is only valid for login_succeeded")
        return self


class AuthEvent(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    project_id: str
    request_id: str
    auth_ref: str
    kind: AuthEventKind
    actor_id: str
    idempotency_key: UUID
    occurred_at: str
    received_at: str
    state: AuthEventState
    attempt_count: int = 0
    next_attempt_at: str | None = None
    claimed_by: str | None = None
    claim_expires_at: str | None = None
    processed_at: str | None = None
    outcome_code: str | None = None
    capture_generation: int | None = None


class AuthGraphIntentRequest(BaseModel):
    """Dispatcher-only source-keyed graph intent mutation."""

    model_config = {"extra": "forbid"}

    project_id: str
    source_key: str
    source_fact_ids: list[str] = Field(min_length=1)
    description: str
    creator: str = "operator.auth"
    worker: str = "operator.auth"

    @field_validator("project_id", "source_key", "description", "creator", "worker")
    @classmethod
    def validate_graph_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("source_fact_ids")
    @classmethod
    def validate_graph_sources(cls, value: list[str]) -> list[str]:
        values = [item.strip() for item in value]
        if any(not item for item in values):
            raise ValueError("source fact ids must not be empty")
        return values


class AuthGraphConcludeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    project_id: str
    intent_source_key: str
    fact_source_key: str
    worker: str = "operator.auth"
    description: str

    @field_validator("project_id", "intent_source_key", "fact_source_key", "worker", "description")
    @classmethod
    def validate_conclusion_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class AuthGraphOutboxAckRequest(BaseModel):
    model_config = {"extra": "forbid"}

    event_id: str
    dispatcher_id: str = "dispatcher"
    state: Literal["fact_created"]
    intent_id: str | None = None
    fact_id: str | None = None
    outcome_code: AuthEventOutcome | None = None

    @field_validator("event_id", "dispatcher_id", "intent_id", "fact_id")
    @classmethod
    def validate_ack_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class AuthGraphOutbox(BaseModel):
    model_config = {"extra": "forbid"}

    event_id: str
    effect_key: str
    project_id: str
    request_id: str
    auth_ref: str
    intent_source_key: str
    fact_source_key: str
    fact_kind: Literal["AuthSessionVerified", "AuthSessionInvalid"]
    state: Literal["pending", "intent_created", "fact_created"]
    intent_id: str | None = None
    fact_id: str | None = None
    outcome_code: str | None = None
    created_at: str
    updated_at: str


class AuthHelperRequestView(BaseModel):
    """Strict, helper-scoped projection; never expose reason or failure details."""

    model_config = {"extra": "forbid"}

    id: str
    auth_ref: str
    login_url: str | None = None
    status: AuthRequestStatus
    # Returned only to the actor that owns a claimed request; omitted while null.
    helper_actor_id: str | None = None


class AuthCredential(BaseModel):
    model_config = {"extra": "forbid"}

    id: int
    token_digest: str
    actor_id: str
    scopes: list[str]
    project_allowlist: list[str]
    not_before: str
    expires_at: str | None = None
    replaced_by: str | None = None
    created_at: str


class AuthDeploymentSnapshot(BaseModel):
    """Dispatcher-owned deployment material used only by the internal bootstrap RPC."""

    model_config = {"extra": "forbid"}

    dispatcher_token: str | None = None
    helper_token: str | None = None
    helper_actor_id: str = "helper"
    helper_scopes: list[str] = Field(
        default_factory=lambda: ["helper.event.submit", "helper.request.read"]
    )
    helper_project_allowlist: list[str] = Field(default_factory=list)
    targets: dict[str, str] = Field(default_factory=dict)
    target_roles: dict[str, str] = Field(default_factory=dict)
    target_reasons: dict[str, str] = Field(default_factory=dict)

    @field_validator("dispatcher_token", "helper_token")
    @classmethod
    def validate_optional_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value if value else None

    @field_validator("helper_actor_id")
    @classmethod
    def validate_actor_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("helper_actor_id must not be empty")
        return value

    @field_validator("helper_scopes", "helper_project_allowlist")
    @classmethod
    def validate_string_lists(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("values must not be empty")
        return sorted(set(cleaned))

    @model_validator(mode="after")
    def validate_distinct_credentials(self) -> "AuthDeploymentSnapshot":
        if self.dispatcher_token is not None and self.dispatcher_token == self.helper_token:
            raise ValueError("dispatcher_token and helper_token must be different")
        if self.helper_token is not None and not self.helper_scopes:
            raise ValueError("helper_scopes must not be empty when helper_token is set")
        return self

    @field_validator("targets")
    @classmethod
    def validate_target_urls(cls, value: dict[str, str]) -> dict[str, str]:
        from urllib.parse import urlparse

        cleaned: dict[str, str] = {}
        for auth_ref, login_url in value.items():
            ref = auth_ref.strip()
            url = login_url.strip()
            parsed = urlparse(url)
            if not ref or parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("targets must contain non-empty auth refs and HTTPS login URLs")
            if ref in cleaned:
                raise ValueError("targets must not contain duplicate auth refs")
            cleaned[ref] = url
        return cleaned

    @field_validator("target_roles", "target_reasons")
    @classmethod
    def validate_target_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, item in value.items():
            key = key.strip()
            item = item.strip()
            if not key or not item:
                raise ValueError("target metadata must contain non-empty values")
            cleaned[key] = item
        return cleaned
