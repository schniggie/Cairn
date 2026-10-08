"""Research workbench storage, independent of the archived campaign lifecycle."""
RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_sessions (
 id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
 objective TEXT NOT NULL, url TEXT, repo TEXT,
 mode TEXT NOT NULL CHECK(mode IN ('web','code','combined')),
 status TEXT NOT NULL CHECK(status IN ('queued','running','pause_requested','paused','waiting_input','completed','failed')),
 phase INTEGER NOT NULL DEFAULT 0 CHECK(phase BETWEEN 0 AND 3),
 authorization_revision INTEGER NOT NULL DEFAULT 1, authorization_json TEXT NOT NULL,
 budget_json TEXT NOT NULL, usage_json TEXT NOT NULL, next_direction TEXT NOT NULL DEFAULT '',
 latest_error TEXT, stack_json TEXT NOT NULL DEFAULT '[]', event_seq INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_expires_at TEXT, worker_session_id TEXT, cursor_json TEXT NOT NULL DEFAULT '{}',
 current_run_id TEXT,
 cost_pending_check INTEGER NOT NULL DEFAULT 0,
 retry_count INTEGER NOT NULL DEFAULT 0,
 latest_failure_json TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_queue ON research_sessions(status,created_at);
CREATE TABLE IF NOT EXISTS research_events (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
 time TEXT NOT NULL, detail_json TEXT, evidence_ids_json TEXT NOT NULL DEFAULT '[]', finding_id TEXT,
 UNIQUE(session_id,seq)
);
CREATE TABLE IF NOT EXISTS research_assets (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, time TEXT NOT NULL,
 metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(session_id,kind,value)
);
CREATE TABLE IF NOT EXISTS research_evidence (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_findings (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 title TEXT NOT NULL, description TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
 evidence_ids_json TEXT NOT NULL DEFAULT '[]', impact TEXT NOT NULL DEFAULT '', limitations TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_authorizations (
 session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(session_id,revision)
);
CREATE TABLE IF NOT EXISTS research_experiences (
 id TEXT PRIMARY KEY,
 source_session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 scope_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('finding','lesson')),
 content TEXT NOT NULL, ref TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_experiences_scope ON research_experiences(scope_key,created_at);
CREATE TABLE IF NOT EXISTS research_changewatch (
 scope_key TEXT PRIMARY KEY,
 fingerprint TEXT NOT NULL,
 session_id TEXT REFERENCES research_sessions(id) ON DELETE SET NULL,
 checked_at TEXT NOT NULL, changed_at TEXT
);
"""

RESEARCH_EXPERIENCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_experiences (
 id TEXT PRIMARY KEY,
 source_session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 scope_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('finding','lesson')),
 content TEXT NOT NULL, ref TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_experiences_scope ON research_experiences(scope_key,created_at);
"""


CHANGE_WATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_changewatch (
 scope_key TEXT PRIMARY KEY,
 fingerprint TEXT NOT NULL,
 session_id TEXT REFERENCES research_sessions(id) ON DELETE SET NULL,
 checked_at TEXT NOT NULL, changed_at TEXT
);
"""


RESEARCH_REPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_reports (
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 event_seq INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 markdown TEXT NOT NULL,
 projection_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_report_history ON research_reports(session_id,created_at,id);
CREATE TRIGGER IF NOT EXISTS research_reports_immutable
BEFORE UPDATE ON research_reports
BEGIN
 SELECT RAISE(ABORT, 'Research report snapshots are immutable');
END;
"""


RESEARCH_RUN_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_run_accounts (
 run_id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 worker_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 finished_at TEXT,
 steps INTEGER NOT NULL DEFAULT 0,
 requests INTEGER NOT NULL DEFAULT 0,
 elapsed_seconds INTEGER NOT NULL DEFAULT 0,
 cost_usd REAL NOT NULL DEFAULT 0,
 cost_status TEXT NOT NULL DEFAULT 'pending_check' CHECK(cost_status IN ('pending_check','recorded')),
 settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
 committed INTEGER NOT NULL DEFAULT 0 CHECK(committed IN (0,1))
);
CREATE INDEX IF NOT EXISTS research_run_history ON research_run_accounts(session_id,created_at,run_id);
"""


RESEARCH_WORKER_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_worker_runtime (
 worker_id TEXT PRIMARY KEY,
 hostname TEXT NOT NULL,
 started_at TEXT NOT NULL,
 last_heartbeat_at TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'idle' CHECK(state IN ('idle','working')),
 current_session_id TEXT
);
"""


RESEARCH_IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_identity_keys (
 id INTEGER PRIMARY KEY CHECK(id=1),
 fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_identities (
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 label TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('account','basic','headers','cookies')),
 origin TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL CHECK(status IN ('active','revoked')),
 ciphertext BLOB,
 nonce BLOB,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK((status='active' AND ciphertext IS NOT NULL AND nonce IS NOT NULL)
    OR (status='revoked' AND ciphertext IS NULL AND nonce IS NULL))
);
CREATE INDEX IF NOT EXISTS research_identity_project ON research_identities(session_id,created_at,id);
"""


RESEARCH_SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_source_snapshots (
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
 repo TEXT NOT NULL,
 authorization_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 digest TEXT NOT NULL,
 file_count INTEGER NOT NULL,
 total_bytes INTEGER NOT NULL,
 languages_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('complete','partial')),
 limitations_json TEXT NOT NULL,
 omissions_json TEXT NOT NULL,
 limits_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_source_history ON research_source_snapshots(session_id,created_at,id);
CREATE TABLE IF NOT EXISTS research_source_files (
 snapshot_id TEXT NOT NULL REFERENCES research_source_snapshots(id) ON DELETE CASCADE,
 path TEXT NOT NULL,
 content TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 bytes INTEGER NOT NULL,
 lines INTEGER NOT NULL,
 language TEXT NOT NULL,
 PRIMARY KEY(snapshot_id,path)
);
CREATE TRIGGER IF NOT EXISTS research_source_snapshots_immutable
BEFORE UPDATE ON research_source_snapshots
BEGIN
 SELECT RAISE(ABORT, 'Source snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS research_source_files_immutable
BEFORE UPDATE ON research_source_files
BEGIN
 SELECT RAISE(ABORT, 'Source snapshot files are immutable');
END;
"""
