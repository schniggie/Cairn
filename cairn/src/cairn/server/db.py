from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

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
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    project_kind TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    difficulty TEXT,
    backend TEXT,
    project_root TEXT,
    started_at TEXT,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    type TEXT,
    confidence TEXT,
    locations TEXT,
    code_version TEXT,
    evidence TEXT,
    verifies TEXT,
    intent_id TEXT,
    batch_id TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    concluded_as TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    task_kind TEXT,
    poc_brief TEXT,
    fire_status TEXT,
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

CREATE TABLE IF NOT EXISTS init_files (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    encoding TEXT NOT NULL DEFAULT 'utf-8',
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    action_id TEXT,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    intent_id TEXT,
    worker TEXT NOT NULL,
    phase TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    decision TEXT,
    rule_id TEXT,
    reason TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_project_created
ON audit_events(project_id, created_at, event_id);

CREATE INDEX IF NOT EXISTS idx_audit_intent_created
ON audit_events(project_id, intent_id, created_at, event_id);

CREATE INDEX IF NOT EXISTS idx_audit_action
ON audit_events(action_id, created_at, event_id);

CREATE TABLE IF NOT EXISTS runtime_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    phase TEXT,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    worker TEXT,
    intent_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_events_project_id_id
ON runtime_events(project_id, id);

CREATE TABLE IF NOT EXISTS http_records (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT,
    worker TEXT,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    request_headers_json TEXT NOT NULL,
    request_body TEXT,
    response_status INTEGER,
    response_headers_json TEXT NOT NULL,
    response_body TEXT,
    significance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_http_records_project_created
ON http_records(project_id, created_at);

CREATE TABLE IF NOT EXISTS ctf_config (
    mode TEXT NOT NULL DEFAULT 'manual',
    adapter TEXT NOT NULL DEFAULT 'ctfd',
    base_url TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    team_name TEXT NOT NULL DEFAULT '',
    flag_regex TEXT NOT NULL DEFAULT '(?:DASCTF|flag)\\{[^}]+\\}',
    auto_submit INTEGER NOT NULL DEFAULT 1,
    max_concurrent INTEGER NOT NULL DEFAULT 2,
    poll_interval INTEGER NOT NULL DEFAULT 10,
    env_poll_interval INTEGER NOT NULL DEFAULT 5,
    env_timeout INTEGER NOT NULL DEFAULT 180,
    submission_max_retries INTEGER NOT NULL DEFAULT 5,
    rate_limit_backoff INTEGER NOT NULL DEFAULT 30,
    model_base_url TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    model_api_key TEXT NOT NULL DEFAULT '',
    last_model_error TEXT,
    model_health_at TEXT,
    last_sync_at TEXT,
    bridge_heartbeat_at TEXT,
    bridge_error TEXT,
    sync_requested INTEGER NOT NULL DEFAULT 0,
    budget_easy INTEGER NOT NULL DEFAULT 12,
    budget_medium INTEGER NOT NULL DEFAULT 25,
    budget_hard INTEGER NOT NULL DEFAULT 40
);

INSERT OR IGNORE INTO ctf_config (rowid) VALUES (1);

CREATE TABLE IF NOT EXISTS ctf_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    points INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    hints TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'queued',
    project_id TEXT,
    last_flag TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    needs_refresh INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS base_knowledge (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS verify_controls (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    kill_requested INTEGER NOT NULL DEFAULT 0,
    kill_requested_at TEXT,
    kill_actor TEXT,
    kill_reason TEXT
);

CREATE TABLE IF NOT EXISTS proxy_traffic (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT,
    request TEXT NOT NULL,
    response TEXT,
    baseline TEXT,
    status TEXT NOT NULL DEFAULT 'recorded',
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_project_columns(conn)
        _ensure_intent_columns(conn)
        _ensure_ctf_columns(conn)
        _ensure_research_schema(conn)
        _ensure_fact_columns(conn)
        _ensure_base_knowledge_table(conn)
        _ensure_verify_tables(conn)
        _ensure_auth_tables(conn)
        from cairn.server.services import bootstrap_auth_deployment

        bootstrap_auth_deployment(conn)


def _ensure_base_knowledge_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS base_knowledge (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            version INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT
        )"""
    )


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )
    if "started_at" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN started_at TEXT")
        conn.execute(
            """
            UPDATE projects
            SET started_at = COALESCE(
                (SELECT MIN(intents.created_at) FROM intents WHERE intents.project_id = projects.id),
                reason_started_at
            )
            WHERE started_at IS NULL
            """
        )
    if "difficulty" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN difficulty TEXT")
    if "project_kind" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN project_kind TEXT NOT NULL DEFAULT 'general'")
    if "backend" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN backend TEXT")
    if "project_root" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN project_root TEXT")


def _ensure_intent_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
    if "concluded_as" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN concluded_as TEXT")
    if "retry_count" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    for col_name, col_type in (
        ("task_kind", "TEXT"),
        ("poc_brief", "TEXT"),
        ("fire_status", "TEXT"),
    ):
        if col_name not in columns:
            conn.execute(f"ALTER TABLE intents ADD COLUMN {col_name} {col_type}")


def _ensure_research_schema(conn: sqlite3.Connection) -> None:
    from cairn.server.research_schema import (
        CHANGE_WATCH_SCHEMA,
        RESEARCH_EXPERIENCES_SCHEMA,
        RESEARCH_IDENTITY_SCHEMA,
        RESEARCH_REPORT_SCHEMA,
        RESEARCH_RUN_ACCOUNTS_SCHEMA,
        RESEARCH_SCHEMA,
        RESEARCH_SOURCE_SCHEMA,
        RESEARCH_WORKER_RUNTIME_SCHEMA,
    )

    for script in (
        RESEARCH_SCHEMA,
        RESEARCH_EXPERIENCES_SCHEMA,
        CHANGE_WATCH_SCHEMA,
        RESEARCH_REPORT_SCHEMA,
        RESEARCH_RUN_ACCOUNTS_SCHEMA,
        RESEARCH_WORKER_RUNTIME_SCHEMA,
        RESEARCH_IDENTITY_SCHEMA,
        RESEARCH_SOURCE_SCHEMA,
    ):
        conn.executescript(script)


def _ensure_ctf_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ctf_config'"
    ).fetchall()
    if rows:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(ctf_config)")}
        for column, ddl in (
            ("sync_requested", "ALTER TABLE ctf_config ADD COLUMN sync_requested INTEGER NOT NULL DEFAULT 0"),
            ("bridge_heartbeat_at", "ALTER TABLE ctf_config ADD COLUMN bridge_heartbeat_at TEXT"),
            ("bridge_error", "ALTER TABLE ctf_config ADD COLUMN bridge_error TEXT"),
            ("env_poll_interval", "ALTER TABLE ctf_config ADD COLUMN env_poll_interval INTEGER NOT NULL DEFAULT 5"),
            ("env_timeout", "ALTER TABLE ctf_config ADD COLUMN env_timeout INTEGER NOT NULL DEFAULT 180"),
            ("submission_max_retries", "ALTER TABLE ctf_config ADD COLUMN submission_max_retries INTEGER NOT NULL DEFAULT 5"),
            ("rate_limit_backoff", "ALTER TABLE ctf_config ADD COLUMN rate_limit_backoff INTEGER NOT NULL DEFAULT 30"),
            ("model_base_url", "ALTER TABLE ctf_config ADD COLUMN model_base_url TEXT NOT NULL DEFAULT ''"),
            ("model_name", "ALTER TABLE ctf_config ADD COLUMN model_name TEXT NOT NULL DEFAULT ''"),
            ("model_api_key", "ALTER TABLE ctf_config ADD COLUMN model_api_key TEXT NOT NULL DEFAULT ''"),
            ("last_model_error", "ALTER TABLE ctf_config ADD COLUMN last_model_error TEXT"),
            ("model_health_at", "ALTER TABLE ctf_config ADD COLUMN model_health_at TEXT"),
            ("budget_easy", "ALTER TABLE ctf_config ADD COLUMN budget_easy INTEGER NOT NULL DEFAULT 12"),
            ("budget_medium", "ALTER TABLE ctf_config ADD COLUMN budget_medium INTEGER NOT NULL DEFAULT 25"),
            ("budget_hard", "ALTER TABLE ctf_config ADD COLUMN budget_hard INTEGER NOT NULL DEFAULT 40"),
        ):
            if column not in columns:
                conn.execute(ddl)

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ctf_challenges'"
    ).fetchall()
    if rows:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(ctf_challenges)")}
        if "needs_refresh" not in columns:
            conn.execute("ALTER TABLE ctf_challenges ADD COLUMN needs_refresh INTEGER NOT NULL DEFAULT 0")


def _ensure_fact_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
    new_columns = [
        ("type", "TEXT"),
        ("confidence", "TEXT"),
        ("locations", "TEXT"),
        ("code_version", "TEXT"),
        ("evidence", "TEXT"),
        ("verifies", "TEXT"),
        ("intent_id", "TEXT"),
        ("batch_id", "TEXT"),
        ("oracle_draft", "TEXT"),
        ("payload_draft", "TEXT"),
    ]
    for col_name, col_type in new_columns:
        if col_name not in columns:
            conn.execute(f"ALTER TABLE facts ADD COLUMN {col_name} {col_type}")


def _ensure_verify_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS verify_controls (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            kill_requested INTEGER NOT NULL DEFAULT 0,
            kill_requested_at TEXT,
            kill_actor TEXT,
            kill_reason TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS proxy_traffic (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            intent_id TEXT,
            request TEXT NOT NULL,
            response TEXT,
            baseline TEXT,
            status TEXT NOT NULL DEFAULT 'recorded',
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id)
        )"""
    )


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


AUTH_TABLES = """\
CREATE TABLE IF NOT EXISTS auth_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_fact_ids TEXT NOT NULL,
    auth_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    login_url TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    failure_reason TEXT,
    helper_actor_id TEXT,
    expires_at TEXT,
    expiry_generation INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_auth_requests_project ON auth_requests (project_id, auth_ref);

CREATE TABLE IF NOT EXISTS auth_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    auth_ref TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('launch_requested', 'browser_opened', 'login_succeeded', 'login_failed')),
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'retryable', 'applied', 'rejected')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    claimed_by TEXT,
    claim_expires_at TEXT,
    processed_at TEXT,
    outcome_code TEXT,
    capture_generation INTEGER,
    UNIQUE (actor_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_auth_events_queue ON auth_events (state, next_attempt_at, received_at);

CREATE TABLE IF NOT EXISTS auth_lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    UNIQUE (request_id, event_id, kind)
);

CREATE TABLE IF NOT EXISTS auth_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_digest TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    project_allowlist TEXT NOT NULL,
    not_before TEXT NOT NULL,
    expires_at TEXT,
    replaced_by TEXT,
    created_at TEXT NOT NULL,
    deployment_owned INTEGER NOT NULL DEFAULT 0,
    deployment_slot TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_credentials_actor ON auth_credentials (actor_id);

CREATE TABLE IF NOT EXISTS auth_credential_cutovers (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    acknowledged_at TEXT NOT NULL,
    revoked_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_target_configs (
    auth_ref TEXT PRIMARY KEY,
    login_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_target_metadata (
    auth_ref TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    request_reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_graph_outbox (
    event_id TEXT PRIMARY KEY REFERENCES auth_events(id) ON DELETE CASCADE,
    effect_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    auth_ref TEXT NOT NULL,
    intent_source_key TEXT NOT NULL UNIQUE,
    fact_source_key TEXT NOT NULL UNIQUE,
    fact_kind TEXT NOT NULL CHECK (fact_kind IN ('AuthSessionVerified', 'AuthSessionInvalid')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'intent_created', 'fact_created')),
    intent_id TEXT,
    fact_id TEXT,
    outcome_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_graph_outbox_pending
    ON auth_graph_outbox (state, updated_at);
"""


def _ensure_auth_tables(conn: sqlite3.Connection) -> None:
    """Create auth-control tables and source-key columns without rewriting SCHEMA."""
    conn.executescript(AUTH_TABLES)
    _ensure_settings_columns(conn)
    _ensure_auth_request_columns(conn)
    _ensure_auth_credential_columns(conn)
    _ensure_auth_schema(conn)
    _ensure_graph_columns(conn)


def _ensure_settings_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "auth_claim_ttl" not in columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN auth_claim_ttl INTEGER NOT NULL DEFAULT 300"
        )
    if "auth_request_ttl" not in columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN auth_request_ttl INTEGER NOT NULL DEFAULT 1800"
        )
    if "auth_control_plane_mode" not in columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN auth_control_plane_mode TEXT NOT NULL DEFAULT 'legacy'"
        )


def _ensure_auth_request_columns(conn: sqlite3.Connection) -> None:
    """Add Phase 1 auth request columns to databases created by older Cairn versions."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_requests)")}
    if "helper_actor_id" not in columns:
        conn.execute("ALTER TABLE auth_requests ADD COLUMN helper_actor_id TEXT")
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE auth_requests ADD COLUMN expires_at TEXT")
    if "expiry_generation" not in columns:
        conn.execute(
            "ALTER TABLE auth_requests ADD COLUMN expiry_generation INTEGER NOT NULL DEFAULT 1"
        )

    ttl_row = conn.execute(
        "SELECT auth_request_ttl FROM settings WHERE rowid = 1"
    ).fetchone()
    ttl = int(ttl_row["auth_request_ttl"]) if ttl_row is not None else 1800
    rows = conn.execute(
        "SELECT id, created_at FROM auth_requests WHERE expires_at IS NULL"
    ).fetchall()
    for row in rows:
        expires_at = _expiry_for(row["created_at"], ttl)
        conn.execute(
            "UPDATE auth_requests SET expires_at = ? WHERE id = ?",
            (expires_at, row["id"]),
        )


def _ensure_auth_credential_columns(conn: sqlite3.Connection) -> None:
    """Add deployment ownership metadata to pre-existing credential tables."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_credentials)")}
    if "deployment_owned" not in columns:
        conn.execute(
            "ALTER TABLE auth_credentials ADD COLUMN deployment_owned INTEGER NOT NULL DEFAULT 0"
        )
    if "deployment_slot" not in columns:
        conn.execute("ALTER TABLE auth_credentials ADD COLUMN deployment_slot TEXT")

    # Before deployment ownership was recorded, bootstrap_auth_credentials used
    # the canonical actor/scope metadata below. Only those unambiguous rows are
    # adopted; operator credentials with similar scopes remain manual.
    rows = conn.execute(
        "SELECT id, actor_id, scopes, project_allowlist FROM auth_credentials "
        "WHERE deployment_owned = 0 AND deployment_slot IS NULL"
    ).fetchall()
    for row in rows:
        try:
            scopes = json.loads(row["scopes"])
            projects = json.loads(row["project_allowlist"])
        except (TypeError, json.JSONDecodeError):
            continue
        slot = None
        if (
            row["actor_id"] == "dispatcher"
            and scopes == ["dispatcher.auth.consume"]
            and projects == ["*"]
        ):
            slot = "dispatcher"
        elif (
            row["actor_id"] == "helper"
            and scopes == ["helper.event.submit", "helper.request.read"]
            and projects == ["*"]
        ):
            slot = "helper"
        if slot is not None:
            conn.execute(
                "UPDATE auth_credentials SET deployment_owned = 1, deployment_slot = ? WHERE id = ?",
                (slot, row["id"]),
            )


def _expiry_for(created_at: str, ttl: int) -> str | None:
    if ttl <= 0:
        return None
    try:
        parsed = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=ttl)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _ensure_auth_schema(conn: sqlite3.Connection) -> None:
    """Backfill deterministic lifecycle records for pre-event auth requests."""
    rows = conn.execute(
        "SELECT id, status, created_at, completed_at FROM auth_requests ORDER BY rowid"
    ).fetchall()
    terminal_statuses = {"completed", "failed", "cancelled", "expired"}
    for row in rows:
        request_id = row["id"]
        existing = conn.execute(
            "SELECT COUNT(*) AS count FROM auth_lifecycle_events WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing["count"]:
            continue
        created_at = row["created_at"]
        conn.execute(
            """
            INSERT INTO auth_lifecycle_events
                (request_id, event_id, sequence, kind, recorded_at, outcome_code)
            VALUES (?, ?, 1, 'created', ?, 'created')
            """,
            (request_id, f"backfill:create:{request_id}", created_at),
        )
        if row["status"] in terminal_statuses:
            recorded_at = row["completed_at"] or created_at
            conn.execute(
                """
                INSERT INTO auth_lifecycle_events
                    (request_id, event_id, sequence, kind, recorded_at, outcome_code)
                VALUES (?, ?, 2, ?, ?, ?)
                """,
                (
                    request_id,
                    f"backfill:terminal:{request_id}",
                    row["status"],
                    recorded_at,
                    row["status"],
                ),
            )


def _ensure_graph_columns(conn: sqlite3.Connection) -> None:
    """Add source-key graph identity and the durable auth graph outbox."""
    fact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
    if "source_key" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN source_key TEXT")
    intent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
    if "source_key" not in intent_columns:
        conn.execute("ALTER TABLE intents ADD COLUMN source_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_source_key ON facts (project_id, source_key) WHERE source_key IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_intents_source_key ON intents (project_id, source_key) WHERE source_key IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_graph_outbox (
            event_id TEXT PRIMARY KEY REFERENCES auth_events(id) ON DELETE CASCADE,
            effect_key TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            auth_ref TEXT NOT NULL,
            intent_source_key TEXT NOT NULL UNIQUE,
            fact_source_key TEXT NOT NULL UNIQUE,
            fact_kind TEXT NOT NULL DEFAULT 'AuthSessionVerified' CHECK (fact_kind IN ('AuthSessionVerified', 'AuthSessionInvalid')),
            state TEXT NOT NULL CHECK (state IN ('pending', 'intent_created', 'fact_created')),
            intent_id TEXT,
            fact_id TEXT,
            outcome_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    outbox_columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_graph_outbox)")}
    if "effect_key" not in outbox_columns:
        conn.execute("ALTER TABLE auth_graph_outbox ADD COLUMN effect_key TEXT")
        conn.execute(
            "UPDATE auth_graph_outbox SET effect_key = 'auth-event:' || event_id WHERE effect_key IS NULL"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_graph_effect_key ON auth_graph_outbox (effect_key)")
    if "fact_kind" not in outbox_columns:
        conn.execute("ALTER TABLE auth_graph_outbox ADD COLUMN fact_kind TEXT NOT NULL DEFAULT 'AuthSessionVerified'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_graph_outbox_pending ON auth_graph_outbox (state, updated_at)"
    )

