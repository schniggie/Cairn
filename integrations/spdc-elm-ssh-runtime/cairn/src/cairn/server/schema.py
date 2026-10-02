SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    auto_reason INTEGER NOT NULL DEFAULT 0,
    allowed_auto_workers_json TEXT,
    default_timeout_seconds INTEGER,
    default_conclude_timeout_seconds INTEGER,
    environment_id TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'fact'
        CHECK (kind IN ('origin','goal','fact','observation','negative_result')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded','retracted')),
    title TEXT,
    description TEXT NOT NULL,
    metadata_json TEXT,
    produced_by_execution_id TEXT,
    produced_by_intent_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    requested_worker TEXT,
    timeout_override_seconds INTEGER,
    conclude_timeout_override_seconds INTEGER,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    concluded_fact_id TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);
INSERT OR IGNORE INTO counters (name, value) VALUES ('agent_context_template', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS work_environments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    backend TEXT NOT NULL,
    ssh_command TEXT,
    workspace_root TEXT,
    cleanup_json TEXT,
    terminal_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_health_status TEXT,
    last_healthcheck_json TEXT
);

CREATE TABLE IF NOT EXISTS environment_provider_endpoints (
    environment_id TEXT NOT NULL REFERENCES work_environments(id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL,
    type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    provider_api TEXT,
    api_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (environment_id, endpoint_id)
);

CREATE TABLE IF NOT EXISTS worker_inventory (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    model_profile TEXT,
    model TEXT,
    model_context_window INTEGER,
    endpoint TEXT,
    task_types_json TEXT NOT NULL,
    max_running INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    allowed_environments_json TEXT,
    question_capability_json TEXT,
    capability_updated_at TEXT,
    capability_source TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_runtime_health (
    environment_id TEXT NOT NULL REFERENCES work_environments(id) ON DELETE CASCADE,
    worker_name TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    endpoint_id TEXT NOT NULL DEFAULT '',
    model_profile_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (status IN ('ok', 'unhealthy', 'unknown')),
    checked_at TEXT NOT NULL,
    stale_after TEXT,
    disabled_until TEXT,
    source TEXT,
    dispatcher_id TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (environment_id, worker_name, worker_type, endpoint_id, model_profile_id)
);

CREATE INDEX IF NOT EXISTS idx_worker_runtime_health_worker
ON worker_runtime_health (worker_name, worker_type, endpoint_id, model_profile_id);

CREATE TABLE IF NOT EXISTS agent_context_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    kind TEXT NOT NULL DEFAULT 'agents_md' CHECK (kind IN ('agents_md')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_agent_contexts (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'agents_md' CHECK (kind IN ('agents_md')),
    enabled INTEGER NOT NULL DEFAULT 1,
    source_template_id TEXT,
    source_template_hash TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS execution_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT,
    branch_id TEXT,
    parent_execution_id TEXT,
    task_type TEXT NOT NULL CHECK (task_type IN ('explore','conclude','reason','question','healthcheck')),
    phase TEXT NOT NULL CHECK (phase IN ('bootstrap','run','followup','healthcheck')),
    session_action TEXT CHECK (session_action IN ('fresh_context','fork_initial','resume_continue','branch_continue')),
    worker_name TEXT,
    worker_type TEXT,
    environment_id TEXT,
    endpoint_id TEXT,
    model_profile_id TEXT,
    workspace TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','leased','running','succeeded','failed','cancelled')),
    leased_by TEXT,
    sink_token TEXT,
    leased_at TEXT,
    lease_expires_at TEXT,
    last_heartbeat_at TEXT,
    control_state TEXT NOT NULL DEFAULT 'normal'
        CHECK (control_state IN ('normal','conclude_requested','abort_requested')),
    control_requested_at TEXT,
    control_reason TEXT,
    remote_session_in_kind TEXT,
    remote_session_in_id TEXT,
    remote_session_in_status TEXT,
    remote_session_out_kind TEXT,
    remote_session_out_id TEXT,
    remote_session_out_status TEXT,
    input_snapshot_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    returncode INTEGER,
    error_code TEXT,
    error_detail TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_execution_id) REFERENCES execution_runs(id)
);

CREATE TABLE IF NOT EXISTS execution_events (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES execution_runs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    project_seq INTEGER NOT NULL,
    cursor TEXT NOT NULL,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('status','stdout','stderr','message','tool','artifact','fact_candidate','session','metric')),
    role TEXT,
    payload_json TEXT NOT NULL,
    event_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(execution_id, seq),
    UNIQUE(execution_id, event_key),
    UNIQUE(project_id, project_seq),
    UNIQUE(project_id, cursor)
);

CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_execution_id TEXT,
    parent_branch_id TEXT,
    anchor_kind TEXT,
    anchor_id TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('source','resume','fork','fresh_context')),
    status TEXT NOT NULL CHECK (status IN ('active','archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_execution_id) REFERENCES execution_runs(id),
    FOREIGN KEY (parent_branch_id) REFERENCES branches(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    produced_by_execution_id TEXT REFERENCES execution_runs(id),
    type TEXT NOT NULL CHECK (type IN ('report','transcript','scan','file','screenshot','other')),
    uri TEXT,
    path TEXT,
    content_hash TEXT,
    summary TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    artifact_id TEXT,
    execution_id TEXT,
    relation TEXT NOT NULL CHECK (relation IN ('supports','contradicts','derived_from')),
    created_at TEXT NOT NULL,
    CHECK (artifact_id IS NOT NULL OR execution_id IS NOT NULL),
    FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
    FOREIGN KEY (execution_id) REFERENCES execution_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS execution_session_locks (
    project_id TEXT NOT NULL,
    remote_session_kind TEXT NOT NULL,
    remote_session_id TEXT NOT NULL,
    execution_id TEXT NOT NULL REFERENCES execution_runs(id) ON DELETE CASCADE,
    branch_id TEXT,
    lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, remote_session_kind, remote_session_id)
);

CREATE INDEX IF NOT EXISTS idx_execution_events_execution_project_seq
ON execution_events (execution_id, project_seq);

CREATE INDEX IF NOT EXISTS idx_execution_events_project_seq
ON execution_events (project_id, project_seq);

CREATE INDEX IF NOT EXISTS idx_execution_runs_project_intent_created
ON execution_runs (project_id, intent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_execution_runs_project_branch_created
ON execution_runs (project_id, branch_id, created_at);

CREATE INDEX IF NOT EXISTS idx_execution_runs_project_status_lease
ON execution_runs (project_id, status, lease_expires_at);

INSERT OR IGNORE INTO work_environments (
    id, label, backend, workspace_root, cleanup_json, terminal_json, created_at, updated_at
) VALUES (
    'docker-default', 'Docker Default', 'docker', NULL, '{"completed_action":"stop"}', '{"mode":"none"}', strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now')
);
"""
