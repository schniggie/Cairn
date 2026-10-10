from __future__ import annotations

import sqlite3

VERSION = "0008_execution_ledger"
DESCRIPTION = "Add v3.2 execution ledger, events, branches, artifacts, and session locks"


def apply(conn: sqlite3.Connection) -> None:
    _add_column(
        conn,
        "intents",
        "concluded_fact_id",
        "TEXT",
    )
    _add_column(
        conn,
        "facts",
        "kind",
        "TEXT NOT NULL DEFAULT 'fact' CHECK (kind IN ('origin','goal','fact','observation','negative_result'))",
    )
    _add_column(
        conn,
        "facts",
        "status",
        "TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted'))",
    )
    _add_column(conn, "facts", "produced_by_execution_id", "TEXT")
    _add_column(conn, "facts", "produced_by_intent_id", "TEXT")
    _add_column(conn, "facts", "created_at", "TEXT")
    _add_column(conn, "facts", "updated_at", "TEXT")
    statements = """
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
            leased_at TEXT,
            lease_expires_at TEXT,
            last_heartbeat_at TEXT,
            control_state TEXT NOT NULL DEFAULT 'normal' CHECK (control_state IN ('normal','conclude_requested','abort_requested')),
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
            event_type TEXT NOT NULL CHECK (event_type IN ('status','stdout','stderr','message','tool','artifact','fact_candidate','session','metric')),
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
        """
    for statement in statements.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not columns:
        return
    if column in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
