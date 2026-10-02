from __future__ import annotations

import sqlite3

VERSION = "0004_worker_inventory_model"
DESCRIPTION = "Track model metadata and public capabilities in worker inventory"


def apply(conn: sqlite3.Connection) -> None:
    _add_column(conn, "worker_inventory", "model", "TEXT")
    _add_column(conn, "worker_inventory", "model_context_window", "INTEGER")
    _add_column(conn, "worker_inventory", "question_capability_json", "TEXT")
    _add_column(conn, "worker_inventory", "capability_updated_at", "TEXT")
    _add_column(conn, "worker_inventory", "capability_source", "TEXT")


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
