from __future__ import annotations

import sqlite3

VERSION = "0001_initial"
DESCRIPTION = "Baseline Cairn server schema"


def apply(conn: sqlite3.Connection) -> None:
    # Historical baseline. Fresh databases are created from schema.SCHEMA.
    return None
