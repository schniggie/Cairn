from __future__ import annotations

import sqlite3

VERSION = "0002_current_additive_schema"
DESCRIPTION = "Add current additive server schema fields and tables"


def apply(conn: sqlite3.Connection) -> None:
    _create_missing_tables(conn)
    _add_missing_columns(conn)
    _seed_defaults(conn)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(ddl)


def _create_missing_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            intent_timeout INTEGER NOT NULL DEFAULT 15,
            reason_timeout INTEGER NOT NULL DEFAULT 15
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            auto_reason INTEGER NOT NULL DEFAULT 0,
            allowed_auto_workers_json TEXT,
            default_timeout_seconds INTEGER,
            default_conclude_timeout_seconds INTEGER,
            environment_id TEXT,
            reason_worker TEXT,
            reason_trigger TEXT,
            reason_started_at TEXT,
            reason_last_heartbeat_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT,
            description TEXT NOT NULL,
            metadata_json TEXT,
            PRIMARY KEY (id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intents (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            to_fact_id TEXT,
            description TEXT NOT NULL,
            creator TEXT NOT NULL,
            worker TEXT,
            requested_worker TEXT,
            timeout_override_seconds INTEGER,
            conclude_timeout_override_seconds INTEGER,
            control_state TEXT NOT NULL DEFAULT 'normal',
            control_requested_at TEXT,
            control_requested_by TEXT,
            control_reason TEXT,
            last_heartbeat_at TEXT,
            created_at TEXT NOT NULL,
            concluded_at TEXT,
            PRIMARY KEY (id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intent_sources (
            intent_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            PRIMARY KEY (intent_id, project_id, fact_id),
            FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hints (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS counters (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scoped_counters (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, kind)
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_inventory (
            name TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            model_profile TEXT,
            endpoint TEXT,
            task_types_json TEXT NOT NULL,
            max_running INTEGER NOT NULL,
            priority INTEGER NOT NULL,
            allowed_environments_json TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    _add_columns(
        conn,
        "projects",
        {
            "environment_id": "ALTER TABLE projects ADD COLUMN environment_id TEXT",
            "auto_reason": "ALTER TABLE projects ADD COLUMN auto_reason INTEGER NOT NULL DEFAULT 0",
            "allowed_auto_workers_json": "ALTER TABLE projects ADD COLUMN allowed_auto_workers_json TEXT",
            "default_timeout_seconds": "ALTER TABLE projects ADD COLUMN default_timeout_seconds INTEGER",
            "default_conclude_timeout_seconds": "ALTER TABLE projects ADD COLUMN default_conclude_timeout_seconds INTEGER",
            "reason_worker": "ALTER TABLE projects ADD COLUMN reason_worker TEXT",
            "reason_trigger": "ALTER TABLE projects ADD COLUMN reason_trigger TEXT",
            "reason_started_at": "ALTER TABLE projects ADD COLUMN reason_started_at TEXT",
            "reason_last_heartbeat_at": "ALTER TABLE projects ADD COLUMN reason_last_heartbeat_at TEXT",
        },
    )
    _add_columns(
        conn,
        "facts",
        {
            "title": "ALTER TABLE facts ADD COLUMN title TEXT",
            "metadata_json": "ALTER TABLE facts ADD COLUMN metadata_json TEXT",
        },
    )
    _add_columns(
        conn,
        "intents",
        {
            "requested_worker": "ALTER TABLE intents ADD COLUMN requested_worker TEXT",
            "timeout_override_seconds": "ALTER TABLE intents ADD COLUMN timeout_override_seconds INTEGER",
            "conclude_timeout_override_seconds": "ALTER TABLE intents ADD COLUMN conclude_timeout_override_seconds INTEGER",
            "control_state": "ALTER TABLE intents ADD COLUMN control_state TEXT NOT NULL DEFAULT 'normal'",
            "control_requested_at": "ALTER TABLE intents ADD COLUMN control_requested_at TEXT",
            "control_requested_by": "ALTER TABLE intents ADD COLUMN control_requested_by TEXT",
            "control_reason": "ALTER TABLE intents ADD COLUMN control_reason TEXT",
        },
    )
    _add_columns(
        conn,
        "work_environments",
        {
            "cleanup_json": "ALTER TABLE work_environments ADD COLUMN cleanup_json TEXT",
            "terminal_json": "ALTER TABLE work_environments ADD COLUMN terminal_json TEXT",
        },
    )


def _seed_defaults(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15)")
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0)")
    conn.execute(
        """
        INSERT OR IGNORE INTO work_environments (
            id, label, backend, workspace_root, cleanup_json, terminal_json, created_at, updated_at
        ) VALUES (
            'docker-default',
            'Docker Default',
            'docker',
            NULL,
            '{"completed_action":"stop"}',
            '{"mode":"none"}',
            strftime('%Y-%m-%dT%H:%M:%SZ','now'),
            strftime('%Y-%m-%dT%H:%M:%SZ','now')
        )
        """
    )
