"""Persistence and helpers for the CTF feature.

The server only stores CTF config / challenge state and exposes the ``/ctf/*``
API. All platform interaction lives in the bridge process.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import CtfChallenge

_CFG_COLUMNS = (
    "mode",
    "adapter",
    "base_url",
    "token",
    "team_name",
    "flag_regex",
    "auto_submit",
    "max_concurrent",
    "poll_interval",
    "env_poll_interval",
    "env_timeout",
    "submission_max_retries",
    "rate_limit_backoff",
    "model_base_url",
    "model_name",
    "model_api_key",
    "last_model_error",
    "model_health_at",
    "last_sync_at",
    "bridge_heartbeat_at",
    "bridge_error",
    "sync_requested",
    "budget_easy",
    "budget_medium",
    "budget_hard",
)

_TOKEN_MASK = "***"


def mask_config(data: dict) -> dict:
    """Return a copy of a config dict with secrets masked for the UI."""
    if data.get("token"):
        data["token"] = _TOKEN_MASK
    if data.get("model_api_key"):
        data["model_api_key"] = _TOKEN_MASK
    return data


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return [str(item) for item in value] if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


# ------------------------------------------------------------------ config


def _row_to_config(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["auto_submit"] = bool(row["auto_submit"])
    data["sync_requested"] = bool(row["sync_requested"])
    return data


def load_config(conn: sqlite3.Connection, *, full: bool = False) -> dict:
    row = conn.execute("SELECT * FROM ctf_config WHERE rowid = 1").fetchone()
    assert row is not None
    data = _row_to_config(row)
    if not full:
        if data["token"]:
            data["token"] = _TOKEN_MASK
        if data["model_api_key"]:
            data["model_api_key"] = _TOKEN_MASK
    return data


def update_config(conn: sqlite3.Connection, body: dict) -> dict:
    """Apply a partial config update. Returns the fresh config (masked)."""
    columns = (
        "adapter",
        "base_url",
        "token",
        "team_name",
        "flag_regex",
        "auto_submit",
        "max_concurrent",
        "poll_interval",
        "env_poll_interval",
        "env_timeout",
        "submission_max_retries",
        "rate_limit_backoff",
        "model_base_url",
        "model_name",
        "model_api_key",
        "budget_easy",
        "budget_medium",
        "budget_hard",
    )
    assignments = []
    params: list = []

    for column in columns:
        if column not in body:
            continue
        value = body[column]
        if column in ("token", "model_api_key"):
            # The frontend echoes the masked secret back unchanged on save.
            if value == _TOKEN_MASK:
                continue
            assignments.append(f"{column} = ?")
            params.append(str(value))
        elif column == "flag_regex":
            try:
                re.compile(value)
            except re.error as exc:
                raise HTTPException(400, f"invalid flag_regex: {exc}") from exc
            assignments.append("flag_regex = ?")
            params.append(str(value))
        elif column == "auto_submit":
            assignments.append("auto_submit = ?")
            params.append(1 if bool(value) else 0)
        elif column in (
            "max_concurrent",
            "poll_interval",
            "env_poll_interval",
            "env_timeout",
            "submission_max_retries",
            "rate_limit_backoff",
        ):
            assignments.append(f"{column} = ?")
            params.append(int(value))
        else:
            assignments.append(f"{column} = ?")
            params.append(str(value))

    if assignments:
        params.append(1)  # rowid
        conn.execute(f"UPDATE ctf_config SET {', '.join(assignments)} WHERE rowid = ?", params)

    row = conn.execute("SELECT * FROM ctf_config WHERE rowid = 1").fetchone()
    return _row_to_config(row)


def set_mode(conn: sqlite3.Connection, mode: str) -> dict:
    if mode not in ("manual", "ctf"):
        raise HTTPException(400, "mode must be 'manual' or 'ctf'")
    conn.execute("UPDATE ctf_config SET mode = ? WHERE rowid = 1", (mode,))
    row = conn.execute("SELECT * FROM ctf_config WHERE rowid = 1").fetchone()
    return _row_to_config(row)


def set_last_sync_at(conn: sqlite3.Connection, synced_at: str) -> None:
    conn.execute(
        "UPDATE ctf_config SET last_sync_at = ?, sync_requested = 0 WHERE rowid = 1",
        (synced_at,),
    )


def request_sync(conn: sqlite3.Connection) -> dict:
    conn.execute("UPDATE ctf_config SET sync_requested = 1 WHERE rowid = 1")
    return load_config(conn, full=False)


def request_sync_ack(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE ctf_config SET sync_requested = 0 WHERE rowid = 1")


def set_bridge_heartbeat(
    conn: sqlite3.Connection,
    *,
    error: str | None,
    model_ok: bool | None = None,
    model_error: str | None = None,
) -> dict:
    now = utcnow()
    conn.execute(
        "UPDATE ctf_config SET bridge_heartbeat_at = ?, bridge_error = ? WHERE rowid = 1",
        (now, error),
    )
    if model_ok is not None:
        conn.execute(
            "UPDATE ctf_config SET model_health_at = ?, last_model_error = ? WHERE rowid = 1",
            (now, None if model_ok else model_error),
        )
    elif model_error is not None:
        conn.execute(
            "UPDATE ctf_config SET model_health_at = ?, last_model_error = ? WHERE rowid = 1",
            (now, model_error),
        )
    return load_config(conn, full=False)


# -------------------------------------------------------------- challenges


def _row_to_challenge(row: sqlite3.Row) -> CtfChallenge:
    return CtfChallenge(
        id=row["id"],
        external_id=row["external_id"],
        title=row["title"],
        category=row["category"],
        points=row["points"],
        description=row["description"],
        target=row["target"],
        attachments=_parse_json_list(row["attachments"]),
        hints=_parse_json_list(row["hints"]),
        status=row["status"],
        project_id=row["project_id"],
        last_flag=row["last_flag"],
        attempt_count=row["attempt_count"],
        needs_refresh=bool(row["needs_refresh"]) if "needs_refresh" in row.keys() else False,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_VALID_STATUSES = {
    "queued",
    "solving",
    "submitted",
    "solved",
    "wrong_flag",
    "rate_limited",
    "paused",
    "failed",
}

# Statuses the bridge considers "in flight" (count toward max_concurrent).
_ACTIVE_STATUSES = {"solving", "submitted", "rate_limited", "wrong_flag"}

# Statuses that may be retried (reset back to queued).
_RETRYABLE_STATUSES = {"failed", "paused", "wrong_flag", "rate_limited", "submitted"}


def list_challenges(conn: sqlite3.Connection) -> list[CtfChallenge]:
    rows = conn.execute(
        "SELECT * FROM ctf_challenges ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [_row_to_challenge(r) for r in rows]


def get_challenge_or_404(conn: sqlite3.Connection, challenge_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM ctf_challenges WHERE id = ?", (challenge_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Challenge not found")
    return row


def create_challenge(conn: sqlite3.Connection, body: dict) -> CtfChallenge:
    external_id = str(body.get("external_id") or "").strip()
    if not external_id:
        raise HTTPException(400, "external_id is required")
    existing = conn.execute(
        "SELECT 1 FROM ctf_challenges WHERE external_id = ?", (external_id,)
    ).fetchone()
    if existing is not None:
        raise HTTPException(409, f"Challenge {external_id} already exists")

    now = body.get("created_at") or utcnow()
    _validate_new_status(body.get("status", "queued"))
    conn.execute(
        """
        INSERT INTO ctf_challenges
            (external_id, title, category, points, description, target,
             attachments, hints, status, project_id, last_flag, attempt_count,
             needs_refresh, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            external_id,
            str(body.get("title") or external_id),
            str(body.get("category") or ""),
            int(body.get("points") or 0),
            str(body.get("description") or ""),
            str(body.get("target") or ""),
            json.dumps(body.get("attachments") or []),
            json.dumps(body.get("hints") or []),
            str(body.get("status") or "queued"),
            body.get("project_id"),
            body.get("last_flag"),
            int(body.get("attempt_count") or 0),
            1 if bool(body.get("needs_refresh")) else 0,
            now,
            body.get("updated_at") or now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM ctf_challenges WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert row is not None
    return _row_to_challenge(row)


def _validate_new_status(status: str) -> None:
    if status not in _VALID_STATUSES:
        raise HTTPException(400, f"invalid status: {status}")


def update_challenge(conn: sqlite3.Connection, challenge_id: int, body: dict) -> CtfChallenge:
    get_challenge_or_404(conn, challenge_id)

    columns = (
        "title",
        "category",
        "points",
        "description",
        "target",
        "attachments",
        "hints",
        "status",
        "project_id",
        "last_flag",
        "attempt_count",
        "needs_refresh",
        "created_at",
        "updated_at",
    )
    assignments = []
    params: list = []

    for column in columns:
        if column not in body:
            continue
        value = body[column]
        if column == "status":
            _validate_new_status(value)
            assignments.append("status = ?")
            params.append(value)
        elif column in ("points", "attempt_count"):
            assignments.append(f"{column} = ?")
            params.append(int(value))
        elif column in ("attachments", "hints"):
            assignments.append(f"{column} = ?")
            params.append(json.dumps(value if value is not None else []))
        elif column in ("project_id", "last_flag", "created_at", "updated_at"):
            assignments.append(f"{column} = ?")
            params.append(value)
        else:
            assignments.append(f"{column} = ?")
            params.append(str(value))

    if assignments:
        params.append(challenge_id)
        conn.execute(
            f"UPDATE ctf_challenges SET {', '.join(assignments)} WHERE id = ?", params
        )

    return _row_to_challenge(get_challenge_or_404(conn, challenge_id))


# ------------------------------------------------------------ lifecycle


def retry_challenge(conn: sqlite3.Connection, challenge_id: int) -> CtfChallenge:
    """Reset a failed / paused / stuck challenge back to the queue.

    The associated project id is cleared so the bridge builds a fresh project.
    """
    row = get_challenge_or_404(conn, challenge_id)
    if row["status"] not in _RETRYABLE_STATUSES:
        raise HTTPException(409, f"cannot retry challenge in status {row['status']}")
    conn.execute(
        "UPDATE ctf_challenges SET status = 'queued', project_id = NULL, updated_at = ? WHERE id = ?",
        (utcnow(), challenge_id),
    )
    return _row_to_challenge(get_challenge_or_404(conn, challenge_id))


def pause_challenge(conn: sqlite3.Connection, challenge_id: int) -> tuple[CtfChallenge, str | None]:
    """Hold a challenge (and its project) without permanently failing it.

    Returns ``(challenge, project_id)`` so the caller can stop the project too.
    """
    row = get_challenge_or_404(conn, challenge_id)
    if row["status"] in ("solved", "failed", "paused"):
        raise HTTPException(409, f"cannot pause challenge in status {row['status']}")
    project_id = row["project_id"]
    conn.execute(
        "UPDATE ctf_challenges SET status = 'paused', project_id = NULL, updated_at = ? WHERE id = ?",
        (utcnow(), challenge_id),
    )
    return _row_to_challenge(get_challenge_or_404(conn, challenge_id)), project_id


def resume_challenge(conn: sqlite3.Connection, challenge_id: int) -> CtfChallenge:
    """Send a paused challenge back to the queue."""
    row = get_challenge_or_404(conn, challenge_id)
    if row["status"] != "paused":
        raise HTTPException(409, f"cannot resume challenge in status {row['status']}")
    conn.execute(
        "UPDATE ctf_challenges SET status = 'queued', updated_at = ? WHERE id = ?",
        (utcnow(), challenge_id),
    )
    return _row_to_challenge(get_challenge_or_404(conn, challenge_id))


def stop_challenge(conn: sqlite3.Connection, challenge_id: int) -> tuple[CtfChallenge, str | None]:
    """Permanently stop a challenge (marks failed; still retryable)."""
    row = get_challenge_or_404(conn, challenge_id)
    if row["status"] in ("solved", "failed"):
        raise HTTPException(409, f"cannot stop challenge in status {row['status']}")
    project_id = row["project_id"]
    conn.execute(
        "UPDATE ctf_challenges SET status = 'failed', project_id = NULL, updated_at = ? WHERE id = ?",
        (utcnow(), challenge_id),
    )
    return _row_to_challenge(get_challenge_or_404(conn, challenge_id)), project_id


# ---------------------------------------------------------------- status


def status_payload(conn: sqlite3.Connection) -> dict:
    cfg = load_config(conn, full=True)
    counts: dict[str, int] = {status: 0 for status in _VALID_STATUSES}
    total_points_solved = 0
    rows = conn.execute(
        "SELECT status, COALESCE(points, 0) AS points FROM ctf_challenges"
    ).fetchall()
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["status"] == "solved":
            total_points_solved += row["points"]

    bridge_running = False
    heartbeat_at = cfg.get("bridge_heartbeat_at")
    if heartbeat_at:
        try:
            last = datetime.strptime(heartbeat_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            age = (datetime.now(timezone.utc) - last).total_seconds()
            bridge_running = age < max(60, 3 * int(cfg.get("poll_interval") or 10))
        except ValueError:
            bridge_running = False

    return {
        "mode": cfg["mode"],
        "adapter": cfg["adapter"],
        "base_url": cfg["base_url"],
        "connected": bool(cfg["base_url"]),
        "bridge_running": bridge_running,
        "bridge_heartbeat_at": cfg["bridge_heartbeat_at"],
        "bridge_error": cfg["bridge_error"],
        "last_sync_at": cfg["last_sync_at"],
        "auto_submit": cfg["auto_submit"],
        "model_name": cfg["model_name"],
        "model_health_at": cfg["model_health_at"],
        "last_model_error": cfg["last_model_error"],
        "counts": counts,
        "total": len(rows),
        "total_points_solved": total_points_solved,
    }
