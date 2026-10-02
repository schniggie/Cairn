from __future__ import annotations

import sqlite3
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


def _ensure_intent_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
    if "concluded_as" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN concluded_as TEXT")
    if "retry_count" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")


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
