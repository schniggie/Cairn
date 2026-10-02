"""Add sink_token column to execution_runs for v3.7 single-writer contract."""

VERSION = "0010"
DESCRIPTION = "Add sink_token to execution_runs for single-writer enforcement"


def apply(conn) -> None:
    conn.execute("ALTER TABLE execution_runs ADD COLUMN sink_token TEXT")
