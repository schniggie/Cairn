from __future__ import annotations

import sqlite3

VERSION = "0007_worker_runtime_health"
DESCRIPTION = "Track worker runtime health by execution identity"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_runtime_health_worker
        ON worker_runtime_health (worker_name, worker_type, endpoint_id, model_profile_id)
        """
    )
