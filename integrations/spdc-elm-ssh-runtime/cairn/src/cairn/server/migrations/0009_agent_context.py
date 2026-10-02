from __future__ import annotations

import sqlite3

VERSION = "0009_agent_context"
DESCRIPTION = "Add server-managed agent context templates and project snapshots"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('agent_context_template', 0)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_context_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            kind TEXT NOT NULL DEFAULT 'agents_md' CHECK (kind IN ('agents_md')),
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
