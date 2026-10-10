from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable

from cairn.server.schema import SCHEMA

MigrationApply = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    apply: MigrationApply


@dataclass(frozen=True)
class MigrationStatus:
    applied: tuple[str, ...]
    pending: tuple[str, ...]
    latest: str | None


def status(conn: sqlite3.Connection, migrations: Iterable[Migration] | None = None) -> MigrationStatus:
    available = _available(migrations)
    applied = _applied_versions(conn) if _table_exists(conn, "schema_migrations") else ()
    return _status_from(applied, available)


def migrate(conn: sqlite3.Connection, migrations: Iterable[Migration] | None = None) -> MigrationStatus:
    use_fresh_schema_shortcut = migrations is None
    available = _available(migrations)
    had_migration_table = _table_exists(conn, "schema_migrations")
    had_app_tables = _has_app_tables(conn)
    _ensure_migration_table(conn)

    applied = _applied_versions(conn)
    if use_fresh_schema_shortcut and not applied and not had_app_tables:
        _create_fresh_schema(conn, available)
        return status(conn, available)

    for migration in available:
        if migration.version in applied:
            continue
        _apply_migration(conn, migration)
        applied = (*applied, migration.version)

    if not had_migration_table and had_app_tables:
        return status(conn, available)
    return _status_from(applied, available)


def available_migrations() -> tuple[Migration, ...]:
    package = __package__
    package_path = Path(__file__).parent
    migrations: list[Migration] = []
    for module_info in pkgutil.iter_modules([str(package_path)]):
        name = module_info.name
        if not name[:4].isdigit():
            continue
        module = importlib.import_module(f"{package}.{name}")
        migrations.append(_migration_from_module(module))
    return tuple(sorted(migrations, key=lambda migration: migration.version))


def _available(migrations: Iterable[Migration] | None) -> tuple[Migration, ...]:
    if migrations is None:
        return available_migrations()
    return tuple(sorted(migrations, key=lambda migration: migration.version))


def _migration_from_module(module: ModuleType) -> Migration:
    return Migration(
        version=module.VERSION,
        description=module.DESCRIPTION,
        apply=module.apply,
    )


def _status_from(applied: tuple[str, ...], migrations: tuple[Migration, ...]) -> MigrationStatus:
    available_versions = tuple(migration.version for migration in migrations)
    applied_set = set(applied)
    pending = tuple(version for version in available_versions if version not in applied_set)
    latest = available_versions[-1] if available_versions else None
    return MigrationStatus(applied=applied, pending=pending, latest=latest)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _has_app_tables(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND name != 'schema_migrations'
        """
    ).fetchall()
    return bool(rows)


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )


def _applied_versions(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return tuple(row["version"] for row in rows)


def _create_fresh_schema(conn: sqlite3.Connection, migrations: tuple[Migration, ...]) -> None:
    with conn:
        conn.executescript(SCHEMA)
        for migration in migrations:
            _stamp(conn, migration.version)


def _apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    savepoint = "migration_" + "".join(ch if ch.isalnum() else "_" for ch in migration.version)
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        migration.apply(conn)
        _stamp(conn, migration.version)
        conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise


def _stamp(conn: sqlite3.Connection, version: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
