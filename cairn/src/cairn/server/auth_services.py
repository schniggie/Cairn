from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from pydantic import ValidationError

from cairn.server.models import (
    AuthCredential,
    AuthDeploymentSnapshot,
    AuthEvent,
    AuthRequest,
)
from cairn.server.services import (
    check_project_active,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
)

DEPLOYMENT_CREDENTIAL_OVERLAP_SECONDS = 300


@dataclass(frozen=True)
class AuthPrincipal:
    actor_id: str
    scopes: frozenset[str]
    project_allowlist: frozenset[str]
    credential_id: int
    token_digest: str


def _credential_model(row: sqlite3.Row) -> AuthCredential:
    return AuthCredential(
        id=row["id"],
        token_digest=row["token_digest"],
        actor_id=row["actor_id"],
        scopes=json.loads(row["scopes"]),
        project_allowlist=json.loads(row["project_allowlist"]),
        not_before=row["not_before"],
        expires_at=row["expires_at"],
        replaced_by=row["replaced_by"],
        created_at=row["created_at"],
    )


def provision_auth_credential(
    conn: sqlite3.Connection,
    token: str,
    *,
    actor_id: str,
    scopes: Iterable[str],
    project_allowlist: Iterable[str] = (),
    not_before: str | None = None,
    expires_at: str | None = None,
) -> AuthCredential:
    """Store only an operator-provided bearer token digest.

    This function is deliberately an internal provisioning primitive: callers receive
    the metadata model, while the opaque token is never persisted or returned.
    An empty project allowlist means no projects; use ``*`` for an operator-wide
    credential explicitly.
    """
    if not token:
        raise ValueError("token must not be empty")
    actor_id = actor_id.strip()
    scope_values = sorted({str(value).strip() for value in scopes if str(value).strip()})
    project_values = sorted({str(value).strip() for value in project_allowlist if str(value).strip()})
    if not actor_id or not scope_values:
        raise ValueError("actor_id and scopes are required")
    now = not_before or utcnow()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        cursor = conn.execute(
            """
            INSERT INTO auth_credentials
                (token_digest, actor_id, scopes, project_allowlist, not_before, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (digest, actor_id, json.dumps(scope_values), json.dumps(project_values), now, expires_at, utcnow()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("credential already exists") from exc
    row = conn.execute("SELECT * FROM auth_credentials WHERE id = ?", (cursor.lastrowid,)).fetchone()
    assert row is not None
    return _credential_model(row)


def bootstrap_auth_credentials(
    conn: sqlite3.Connection,
    *,
    helper_token: str | None = None,
    dispatcher_token: str | None = None,
    helper_actor_id: str | None = None,
    helper_scopes: Iterable[str] | None = None,
    helper_project_allowlist: Iterable[str] | None = None,
    allow_environment_fallback: bool = True,
) -> None:
    """Provision deployment credentials while keeping opaque tokens out of storage."""
    if allow_environment_fallback:
        helper_token = helper_token if helper_token is not None else os.getenv("CAIRN_AUTH_HELPER_TOKEN")
        dispatcher_token = dispatcher_token if dispatcher_token is not None else os.getenv("CAIRN_AUTH_DISPATCHER_TOKEN")
    if helper_token and dispatcher_token and helper_token.strip() == dispatcher_token.strip():
        raise ValueError("dispatcher_token and helper_token must be different")
    if helper_token:
        helper_actor = helper_actor_id or (
            os.getenv("CAIRN_AUTH_HELPER_ACTOR_ID", "helper")
            if allow_environment_fallback
            else "helper"
        )
        helper_scope_values = (
            helper_scopes
            if helper_scopes is not None
            else _csv_env("CAIRN_AUTH_HELPER_SCOPES", "helper.event.submit,helper.request.read")
            if allow_environment_fallback
            else ["helper.event.submit", "helper.request.read"]
        )
        helper_project_values = (
            helper_project_allowlist
            if helper_project_allowlist is not None
            else _csv_env("CAIRN_AUTH_HELPER_PROJECTS", "*")
            if allow_environment_fallback
            else ["*"]
        )
        _upsert_deployment_credential(
            conn,
            helper_token,
            actor_id=helper_actor,
            scopes=helper_scope_values,
            projects=helper_project_values,
            deployment_slot="helper",
        )
    if dispatcher_token:
        dispatcher_actor = (
            os.getenv("CAIRN_AUTH_DISPATCHER_ACTOR_ID", "dispatcher")
            if allow_environment_fallback
            else "dispatcher"
        )
        dispatcher_scope_values = (
            _csv_env("CAIRN_AUTH_DISPATCHER_SCOPES", "dispatcher.auth.consume")
            if allow_environment_fallback
            else ["dispatcher.auth.consume"]
        )
        dispatcher_project_values = (
            _csv_env("CAIRN_AUTH_DISPATCHER_PROJECTS", "*")
            if allow_environment_fallback
            else ["*"]
        )
        _upsert_deployment_credential(
            conn,
            dispatcher_token,
            actor_id=dispatcher_actor,
            scopes=dispatcher_scope_values,
            projects=dispatcher_project_values,
            deployment_slot="dispatcher",
        )


def bootstrap_auth_deployment(
    conn: sqlite3.Connection,
    *,
    auth_config: object | None = None,
    dispatcher_token: str | None = None,
    target_configs: dict[str, str] | None = None,
    helper_token: str | None = None,
    helper_actor_id: str | None = None,
    helper_scopes: Iterable[str] | None = None,
    helper_project_allowlist: Iterable[str] | None = None,
    target_roles: dict[str, str] | None = None,
    target_reasons: dict[str, str] | None = None,
    allow_environment_fallback: bool = True,
) -> None:
    """Apply one deployment's AuthConfig snapshot atomically.

    Dispatcher owns the AuthConfig. In a split deployment it supplies a serialized
    snapshot through the deployment mechanism; this function accepts that snapshot
    (or the concrete config object in local mode), while never persisting raw tokens.
    """
    snapshot_present = False
    apply_targets = target_configs is not None
    target_roles = target_roles or {}
    target_reasons = target_reasons or {}
    helper_token_env = getattr(auth_config, "helper_token_env", "CAIRN_AUTH_HELPER_TOKEN")

    if auth_config is None and allow_environment_fallback:
        snapshot_raw = os.getenv("CAIRN_AUTH_DEPLOYMENT_SNAPSHOT")
        if snapshot_raw is not None:
            snapshot_present = True
            try:
                parsed: Any = json.loads(snapshot_raw)
                snapshot = AuthDeploymentSnapshot.model_validate(parsed)
            except (json.JSONDecodeError, TypeError, ValidationError) as exc:
                raise ValueError("invalid auth deployment snapshot") from exc
            dispatcher_token = snapshot.dispatcher_token
            helper_token = snapshot.helper_token
            helper_actor_id = snapshot.helper_actor_id
            helper_scopes = snapshot.helper_scopes
            helper_projects = snapshot.helper_project_allowlist
            target_configs = snapshot.targets
            target_roles = snapshot.target_roles
            target_reasons = snapshot.target_reasons
            apply_targets = True
        else:
            helper_token = helper_token if helper_token is not None else os.getenv(helper_token_env)
            dispatcher_token = (
                dispatcher_token
                if dispatcher_token is not None
                else os.getenv("CAIRN_AUTH_DISPATCHER_TOKEN")
            )
            helper_actor_id = helper_actor_id or os.getenv("CAIRN_AUTH_HELPER_ACTOR_ID", "helper")
            helper_scopes = helper_scopes if helper_scopes is not None else _csv_env(
                "CAIRN_AUTH_HELPER_SCOPES", "helper.event.submit,helper.request.read"
            )
            helper_projects = (
                helper_project_allowlist
                if helper_project_allowlist is not None
                else _csv_env("CAIRN_AUTH_HELPER_PROJECTS", "*")
            )
    else:
        helper_token = helper_token if helper_token is not None else (
            os.getenv(helper_token_env) if allow_environment_fallback else None
        )
        helper_actor_id = helper_actor_id if helper_actor_id is not None else getattr(
            auth_config, "helper_actor_id", "helper"
        )
        helper_scopes = helper_scopes if helper_scopes is not None else getattr(
            auth_config, "helper_scopes", None
        )
        helper_projects = (
            helper_project_allowlist
            if helper_project_allowlist is not None
            else getattr(auth_config, "helper_project_allowlist", None)
        )
        if target_configs is None and auth_config is not None:
            target_configs = {
                target.name: target.login_url
                for target in getattr(auth_config, "targets", [])
            }
            target_roles = {
                target.name: target.role for target in getattr(auth_config, "targets", [])
            }
            target_reasons = {
                target.name: target.request_reason
                for target in getattr(auth_config, "targets", [])
            }
            apply_targets = True

    # Validate every field before touching SQLite. This matters for callers that
    # invoke this primitive outside ``db.get_conn``'s transaction wrapper.
    snapshot = AuthDeploymentSnapshot.model_validate(
        {
            "dispatcher_token": dispatcher_token,
            "helper_token": helper_token,
            "helper_actor_id": helper_actor_id or "helper",
            "helper_scopes": list(helper_scopes or ["helper.event.submit", "helper.request.read"]),
            "helper_project_allowlist": list(helper_projects or []),
            "targets": target_configs if target_configs is not None else {},
            "target_roles": target_roles if target_roles is not None else {},
            "target_reasons": target_reasons if target_reasons is not None else {},
        }
    )
    if allow_environment_fallback:
        _apply_legacy_credential_cutover(conn)
    if (
        not snapshot_present
        and auth_config is None
        and dispatcher_token is None
        and helper_token is None
        and not apply_targets
    ):
        # A plain Server restart without deployment material must not revoke the
        # last known deployment authority. Explicit snapshots still reconcile.
        return

    _validate_deployment_collisions(conn, snapshot)
    _reconcile_deployment_credentials(conn, snapshot)
    bootstrap_auth_credentials(
        conn,
        helper_token=snapshot.helper_token,
        dispatcher_token=snapshot.dispatcher_token,
        helper_actor_id=snapshot.helper_actor_id,
        helper_scopes=snapshot.helper_scopes,
        helper_project_allowlist=snapshot.helper_project_allowlist,
        allow_environment_fallback=False,
    )
    if apply_targets:
        bootstrap_auth_target_configs(
            conn,
            snapshot.targets,
            roles=snapshot.target_roles,
            reasons=snapshot.target_reasons,
        )


def _apply_legacy_credential_cutover(conn: sqlite3.Connection) -> None:
    """Honor the server-operator's one-time acknowledgement to revoke legacy rows.

    Pre-ownership credentials are intentionally never inferred from actor/scope
    metadata: custom helper deployments and manually provisioned credentials are
    indistinguishable.  The acknowledgement is therefore an explicit, server-only
    deployment control.  The audit row makes retries idempotent and observable.
    """
    if os.getenv("CAIRN_AUTH_LEGACY_CREDENTIAL_CUTOVER", "").strip().lower() != "revoke":
        return
    existing = conn.execute("SELECT id FROM auth_credential_cutovers WHERE id = 1").fetchone()
    if existing is not None:
        return
    now = utcnow()
    cursor = conn.execute(
        """
        UPDATE auth_credentials
           SET expires_at = ?
         WHERE deployment_owned = 0
           AND (expires_at IS NULL OR expires_at > ?)
        """,
        (now, now),
    )
    conn.execute(
        "INSERT INTO auth_credential_cutovers (id, acknowledged_at, revoked_count) VALUES (1, ?, ?)",
        (now, cursor.rowcount),
    )


def _csv_env(name: str, default: str) -> list[str]:
    return sorted({item.strip() for item in os.getenv(name, default).split(",") if item.strip()})


def _upsert_deployment_credential(
    conn: sqlite3.Connection,
    token: str,
    *,
    actor_id: str,
    scopes: Iterable[str],
    projects: Iterable[str],
    deployment_slot: str,
) -> None:
    actor_id = actor_id.strip()
    scope_values = sorted({str(value).strip() for value in scopes if str(value).strip()})
    project_values = sorted({str(value).strip() for value in projects if str(value).strip()})
    if not actor_id or not scope_values or deployment_slot not in {"dispatcher", "helper"}:
        raise ValueError("invalid deployment credential metadata")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    encoded_scopes = json.dumps(scope_values)
    encoded_projects = json.dumps(project_values)
    now = utcnow()
    existing = conn.execute(
        "SELECT * FROM auth_credentials WHERE token_digest = ?", (digest,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO auth_credentials
                (token_digest, actor_id, scopes, project_allowlist, not_before, created_at,
                 deployment_owned, deployment_slot)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (digest, actor_id, encoded_scopes, encoded_projects, now, now, deployment_slot),
        )
    else:
        existing_scopes = json.loads(existing["scopes"])
        existing_projects = json.loads(existing["project_allowlist"])
        if (
            existing["actor_id"] != actor_id
            or existing_scopes != scope_values
            or existing_projects != project_values
        ):
            raise ValueError("deployment credential digest collision")
        conn.execute(
            """
            UPDATE auth_credentials
               SET deployment_owned = 1, deployment_slot = ?, expires_at = NULL
             WHERE id = ?
            """,
            (deployment_slot, existing["id"]),
        )


def _validate_deployment_collisions(
    conn: sqlite3.Connection, snapshot: AuthDeploymentSnapshot
) -> None:
    desired = (
        ("dispatcher", snapshot.dispatcher_token, "dispatcher", ["dispatcher.auth.consume"], ["*"]),
        (
            "helper",
            snapshot.helper_token,
            snapshot.helper_actor_id,
            snapshot.helper_scopes,
            snapshot.helper_project_allowlist,
        ),
    )
    seen: set[str] = set()
    for slot, token, actor_id, scopes, projects in desired:
        if token is None:
            continue
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if digest in seen:
            raise ValueError("dispatcher and helper tokens must be different")
        seen.add(digest)
        row = conn.execute(
            "SELECT actor_id, scopes, project_allowlist FROM auth_credentials WHERE token_digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            continue
        if (
            row["actor_id"] != actor_id
            or json.loads(row["scopes"]) != sorted(set(scopes))
            or json.loads(row["project_allowlist"]) != sorted(set(projects))
        ):
            raise ValueError(f"deployment credential digest collision for {slot}")


def _reconcile_deployment_credentials(
    conn: sqlite3.Connection, snapshot: AuthDeploymentSnapshot
) -> None:
    desired_digests = {
        hashlib.sha256(token.encode("utf-8")).hexdigest()
        for token in (snapshot.dispatcher_token, snapshot.helper_token)
        if token is not None
    }
    desired_slots = {
        slot
        for slot, token in (("dispatcher", snapshot.dispatcher_token), ("helper", snapshot.helper_token))
        if token is not None
    }
    now = datetime.now(timezone.utc)
    overlap = (now + timedelta(seconds=DEPLOYMENT_CREDENTIAL_OVERLAP_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    rows = conn.execute(
        "SELECT id, token_digest, actor_id, scopes, expires_at, deployment_slot FROM auth_credentials WHERE deployment_owned = 1"
    ).fetchall()
    for row in rows:
        if row["token_digest"] in desired_digests:
            continue
        # A replaced current/previous credential is retained only for the short,
        # fixed overlap. Helper credentials omitted from a snapshot are revoked
        # immediately; the Dispatcher seed gets the same bounded grace window so
        # an in-flight deployment call cannot strand the control plane.
        expires_at = (
            _bounded_expiry(row["expires_at"], overlap)
            if row["deployment_slot"] in desired_slots or row["deployment_slot"] == "dispatcher"
            else utcnow()
        )
        conn.execute(
            "UPDATE auth_credentials SET expires_at = ? WHERE id = ?",
            (expires_at, row["id"]),
        )


def _bounded_expiry(existing: str | None, proposed: str) -> str:
    """Never extend an already shorter credential validity window."""
    if not existing:
        return proposed
    try:
        existing_dt = datetime.fromisoformat(existing.replace("Z", "+00:00"))
        proposed_dt = datetime.fromisoformat(proposed.replace("Z", "+00:00"))
    except ValueError:
        return existing
    if existing_dt.tzinfo is None:
        existing_dt = existing_dt.replace(tzinfo=timezone.utc)
    if proposed_dt.tzinfo is None:
        proposed_dt = proposed_dt.replace(tzinfo=timezone.utc)
    return existing if existing_dt <= proposed_dt else proposed


def bootstrap_auth_target_configs(
    conn: sqlite3.Connection,
    values: dict[str, str] | None = None,
    *,
    roles: dict[str, str] | None = None,
    reasons: dict[str, str] | None = None,
) -> None:
    """Replace target authorities with one validated Dispatcher-owned snapshot."""
    if values is None:
        return
    conn.execute("DELETE FROM auth_target_configs")
    conn.execute("DELETE FROM auth_target_metadata")
    roles = roles or {}
    reasons = reasons or {}
    for auth_ref, login_url in values.items():
        if not isinstance(auth_ref, str) or not isinstance(login_url, str):
            continue
        parsed = urlparse(login_url)
        if parsed.scheme != "https" or not parsed.netloc:
            continue
        conn.execute(
            "INSERT INTO auth_target_configs (auth_ref, login_url) VALUES (?, ?) ON CONFLICT(auth_ref) DO UPDATE SET login_url = excluded.login_url",
            (auth_ref.strip(), login_url),
        )
        auth_ref = auth_ref.strip()
        conn.execute(
            "INSERT INTO auth_target_metadata (auth_ref, role, request_reason) VALUES (?, ?, ?)",
            (auth_ref, roles.get(auth_ref, "user"), reasons.get(auth_ref, "authentication_required")),
        )


def lookup_auth_credential(
    conn: sqlite3.Connection, token: str, *, now: str | None = None
) -> AuthPrincipal | None:
    """Resolve a token by SHA-256 digest and enforce its validity window."""
    if not token:
        return None
    current = now or utcnow()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        SELECT * FROM auth_credentials
        WHERE token_digest = ?
          AND not_before <= ?
          AND (expires_at IS NULL OR expires_at > ?)
        """,
        (digest, current, current),
    ).fetchone()
    if row is None:
        return None
    return AuthPrincipal(
        actor_id=row["actor_id"],
        scopes=frozenset(json.loads(row["scopes"])),
        project_allowlist=frozenset(json.loads(row["project_allowlist"])),
        credential_id=row["id"],
        token_digest=row["token_digest"],
    )


def auth_token_digest(token: str) -> str:
    """Return the persisted representation for an opaque bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rotate_auth_credential(
    conn: sqlite3.Connection,
    old_token: str,
    new_token: str,
    *,
    overlap_seconds: int = 300,
) -> AuthCredential:
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds must be non-negative")
    old_digest = hashlib.sha256(old_token.encode("utf-8")).hexdigest()
    old = conn.execute("SELECT * FROM auth_credentials WHERE token_digest = ?", (old_digest,)).fetchone()
    if old is None:
        raise ValueError("credential not found")
    new = provision_auth_credential(
        conn,
        new_token,
        actor_id=old["actor_id"],
        scopes=json.loads(old["scopes"]),
        project_allowlist=json.loads(old["project_allowlist"]),
    )
    overlap_dt = datetime.now(timezone.utc) + timedelta(seconds=overlap_seconds)
    if old["expires_at"]:
        try:
            old_expiry = datetime.strptime(old["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            old_expiry = datetime.fromisoformat(old["expires_at"].replace("Z", "+00:00"))
            if old_expiry.tzinfo is None:
                old_expiry = old_expiry.replace(tzinfo=timezone.utc)
        overlap_dt = min(overlap_dt, old_expiry)
    overlap = overlap_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE auth_credentials SET expires_at = ?, replaced_by = ? WHERE id = ?",
        (overlap, str(new.id), old["id"]),
    )
    return new


def revoke_auth_credential(conn: sqlite3.Connection, token_or_digest: str) -> bool:
    digest = token_or_digest
    if len(token_or_digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in token_or_digest):
        digest = hashlib.sha256(token_or_digest.encode("utf-8")).hexdigest()
    cursor = conn.execute(
        "UPDATE auth_credentials SET expires_at = ? WHERE token_digest = ?",
        (utcnow(), digest.lower()),
    )
    return cursor.rowcount == 1


def require_auth_principal(
    request: Request,
    conn: sqlite3.Connection,
    *,
    scope: str,
    project_id: str | None = None,
) -> AuthPrincipal:
    require_secure_bearer_transport(request)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token.strip():
        raise HTTPException(401, "Authentication required")
    principal = lookup_auth_credential(conn, token.strip())
    if principal is None:
        raise HTTPException(401, "Authentication required")
    if scope not in principal.scopes and "*" not in principal.scopes:
        raise HTTPException(403, "Forbidden")
    if project_id is not None and "*" not in principal.project_allowlist and project_id not in principal.project_allowlist:
        raise HTTPException(403, "Forbidden")
    return principal


def require_secure_bearer_transport(request: Request) -> None:
    """Require HTTPS unless the request is clearly loopback or proxy-marked HTTPS."""
    forwarded = request.headers.get("forwarded", "")
    forwarded_proto = next(
        (part.split("=", 1)[1].strip(' "') for part in forwarded.split(";") if part.strip().lower().startswith("proto=")),
        "",
    )
    proxy_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    client_host = (request.client.host if request.client else "").strip("[]").lower()
    proxy_marked_https = (proxy_proto == "https" or forwarded_proto.lower() == "https") and _is_trusted_proxy(client_host)
    if request.url.scheme.lower() == "https" or proxy_marked_https:
        return
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(400, "Bearer credentials require HTTPS")


def _is_trusted_proxy(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    configured = os.getenv("CAIRN_TRUSTED_PROXY_NETWORKS", "127.0.0.0/8,::1/128")
    for raw_network in configured.split(","):
        try:
            if address in ipaddress.ip_network(raw_network.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def reject_migrated_helper_raw_listing(request: Request, conn: sqlite3.Connection) -> None:
    """Deny migrated helper credentials while retaining unauthenticated legacy access."""
    if get_auth_control_plane_mode(conn) == "legacy":
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return
    principal = lookup_auth_credential(conn, token.strip())
    if principal is not None and {"helper.request.read", "helper.event.submit"} & principal.scopes:
        raise HTTPException(403, "Forbidden")


def get_auth_control_plane_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT auth_control_plane_mode FROM settings WHERE rowid = 1").fetchone()
    return row["auth_control_plane_mode"] if row is not None else "legacy"


def guard_legacy_auth_mutation(request: Request, conn: sqlite3.Connection) -> None:
    """Apply the migration mode to an old AuthRequest mutation route."""
    mode = get_auth_control_plane_mode(conn)
    if mode == "enforced":
        raise HTTPException(410, "Authentication events are required")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if mode == "dual_write" and scheme.lower() == "bearer" and token:
        principal = lookup_auth_credential(conn, token.strip())
        if principal is not None and (
            "helper.event.submit" in principal.scopes or "helper.request.read" in principal.scopes
        ):
            raise HTTPException(410, "Authentication events are required")


def auth_event_to_model(row: sqlite3.Row) -> AuthEvent:
    from cairn.server.models import AuthEvent

    return AuthEvent(
        id=row["id"], project_id=row["project_id"], request_id=row["request_id"], auth_ref=row["auth_ref"],
        kind=row["kind"], actor_id=row["actor_id"], idempotency_key=row["idempotency_key"], occurred_at=row["occurred_at"],
        received_at=row["received_at"], state=row["state"], attempt_count=row["attempt_count"], next_attempt_at=row["next_attempt_at"],
        claimed_by=row["claimed_by"], claim_expires_at=row["claim_expires_at"], processed_at=row["processed_at"],
        outcome_code=row["outcome_code"], capture_generation=row["capture_generation"],
    )


AUTH_EVENT_CLAIM_SECONDS = 30


AUTH_EVENT_MAX_ATTEMPTS = 3


def ensure_auth_graph_outbox(
    conn: sqlite3.Connection, event: sqlite3.Row, *, now: str | None = None
) -> sqlite3.Row:
    """Create the one durable graph effect for an auth verification event.

    The event id is the idempotency boundary.  Stable source keys allow each graph
    RPC to be retried independently after a process or network failure.
    """
    timestamp = now or utcnow()
    effect_key = f"auth-event:{event['id']}"
    intent_key = f"auth-event:{event['id']}:intent"
    fact_key = f"auth-event:{event['id']}:fact"
    conn.execute(
        """
        INSERT INTO auth_graph_outbox
            (event_id, effect_key, project_id, request_id, auth_ref, intent_source_key,
             fact_source_key, fact_kind, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'AuthSessionVerified', 'pending', ?, ?)
        ON CONFLICT(event_id) DO NOTHING
        """,
        (event["id"], effect_key, event["project_id"], event["request_id"], event["auth_ref"],
         intent_key, fact_key, timestamp, timestamp),
    )
    row = conn.execute("SELECT * FROM auth_graph_outbox WHERE event_id = ?", (event["id"],)).fetchone()
    assert row is not None
    return row


def auth_graph_outbox_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return dict(row)


def ack_auth_graph_outbox(
    conn: sqlite3.Connection,
    event_id: str,
    dispatcher_id: str,
    *,
    state: str,
    intent_id: str | None = None,
    fact_id: str | None = None,
    outcome_code: str | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    """Atomically publish graph progress and finish its owning auth event."""
    outbox = conn.execute("SELECT * FROM auth_graph_outbox WHERE event_id = ?", (event_id,)).fetchone()
    if outbox is None:
        raise HTTPException(404, "Auth graph outbox entry not found")
    event = conn.execute("SELECT * FROM auth_events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        raise HTTPException(404, "Auth event not found")
    request = get_auth_request_or_404(conn, event["request_id"])
    if event["state"] in ("applied", "rejected"):
        return event, request, outbox
    if (
        event["state"] != "claimed" or event["claimed_by"] != dispatcher_id
    ):
        raise HTTPException(409, "Auth event claim is not owned by dispatcher")
    if state == "fact_created":
        if not intent_id or not fact_id:
            raise HTTPException(422, "fact_created acknowledgement requires graph ids")
        if outbox["state"] not in {"fact_created", "intent_created"}:
            raise HTTPException(409, "outbox is not ready for acknowledgement")
        intent = conn.execute(
            "SELECT id, source_key FROM intents WHERE project_id = ? AND id = ?",
            (event["project_id"], intent_id),
        ).fetchone()
        fact = conn.execute(
            "SELECT id, source_key FROM facts WHERE project_id = ? AND id = ?",
            (event["project_id"], fact_id),
        ).fetchone()
        if (
            intent is None or intent["source_key"] != outbox["intent_source_key"]
            or fact is None or fact["source_key"] != outbox["fact_source_key"]
        ):
            raise HTTPException(409, "graph ids do not match auth outbox")
        invalid_codes = {
            "login_failed", "verification_failed", "store_unavailable",
            "capture_mismatch", "unknown_target", "invalid_transition",
        }
        if outcome_code is not None and outcome_code not in invalid_codes and outcome_code != "verified":
            raise HTTPException(422, "unsafe auth outcome")
        now = utcnow()
        conn.execute(
            "UPDATE auth_graph_outbox SET state = 'fact_created', fact_kind = ?, intent_id = ?, fact_id = ?, outcome_code = ?, updated_at = ? WHERE event_id = ?",
            ("AuthSessionInvalid" if outcome_code in invalid_codes else "AuthSessionVerified", intent_id, fact_id, outcome_code or "verified", now, event_id),
        )
        if outcome_code in invalid_codes:
            conn.execute(
                "UPDATE auth_events SET state = 'rejected', processed_at = ?, outcome_code = ?, claimed_by = NULL, claim_expires_at = NULL WHERE id = ? AND state = 'claimed' AND claimed_by = ?",
                (now, outcome_code, event_id, dispatcher_id),
            )
            conn.execute(
                "UPDATE auth_requests SET status = 'failed', completed_at = ?, failure_reason = ?, claimed_by = NULL, claimed_at = NULL, helper_actor_id = NULL WHERE id = ? AND status = 'verifying'",
                (now, outcome_code, request["id"]),
            )
            _append_auth_lifecycle(conn, request["id"], event_id, "failed", outcome_code, recorded_at=now)
        else:
            conn.execute(
                "UPDATE auth_events SET state = 'applied', processed_at = ?, outcome_code = 'verified', claimed_by = NULL, claim_expires_at = NULL WHERE id = ? AND state = 'claimed' AND claimed_by = ?",
                (now, event_id, dispatcher_id),
            )
            conn.execute(
                "UPDATE auth_requests SET status = 'completed', completed_at = ?, failure_reason = NULL, claimed_by = NULL, claimed_at = NULL, helper_actor_id = NULL WHERE id = ? AND status = 'verifying'",
                (now, request["id"]),
            )
            _append_auth_lifecycle(conn, request["id"], event_id, "completed", "verified", recorded_at=now)
    else:
        # A graph outbox can only be acknowledged after its Fact exists.  Invalid
        # outcomes still use state=fact_created with a fixed safe outcome code.
        raise HTTPException(422, "outbox acknowledgement requires fact_created")
    return (
        conn.execute("SELECT * FROM auth_events WHERE id = ?", (event_id,)).fetchone(),
        conn.execute("SELECT * FROM auth_requests WHERE id = ?", (request["id"],)).fetchone(),
        conn.execute("SELECT * FROM auth_graph_outbox WHERE event_id = ?", (event_id,)).fetchone(),
    )


def _append_auth_lifecycle(
    conn: sqlite3.Connection,
    request_id: str,
    event_id: str,
    kind: str,
    outcome_code: str,
    *,
    recorded_at: str | None = None,
) -> bool:
    """Append a lifecycle entry once, returning False for a replay."""
    try:
        conn.execute(
            """
            INSERT INTO auth_lifecycle_events
                (request_id, event_id, sequence, kind, recorded_at, outcome_code)
            SELECT ?, ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ?
              FROM auth_lifecycle_events
             WHERE request_id = ?
            """,
            (request_id, event_id, kind, recorded_at or utcnow(), outcome_code, request_id),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def claim_auth_event_atomic(
    conn: sqlite3.Connection,
    dispatcher_id: str,
    *,
    lease_seconds: int = AUTH_EVENT_CLAIM_SECONDS,
    project_allowlist: frozenset[str] | None = None,
) -> sqlite3.Row | None:
    """Claim the oldest queued event with a short server-clock lease."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    lease_expires = (now_dt + timedelta(seconds=lease_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    params: list[object] = [now]
    allowlist_sql = ""
    if project_allowlist is not None and "*" not in project_allowlist:
        if project_allowlist:
            placeholders = ",".join("?" for _ in project_allowlist)
            allowlist_sql = f" AND project_id IN ({placeholders})"
            params.extend(sorted(project_allowlist))
        else:
            # Empty is an explicit deny-all allowlist; only ``None`` means the
            # direct service caller did not request project filtering.
            allowlist_sql = " AND 0"
    # Select-and-update in one SQLite statement. This avoids the check-then-act
    # race where two Dispatcher connections could both observe the same queued
    # event before either writes its lease.
    update_query = f"""
        UPDATE auth_events
           SET state = 'claimed', claimed_by = ?, claim_expires_at = ?,
               attempt_count = attempt_count + 1
         WHERE id = (
             SELECT id FROM auth_events
              WHERE state IN ('queued', 'retryable')
                AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                {allowlist_sql}
              ORDER BY received_at, rowid
              LIMIT 1
         )
         RETURNING *
    """
    return conn.execute(
        update_query,
        (dispatcher_id, lease_expires, *params),
    ).fetchone()


def recover_auth_event_claims(
    conn: sqlite3.Connection, *, project_allowlist: frozenset[str] | None = None
) -> int:
    """Move expired claims back to retryable, rejecting after three attempts."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = "SELECT * FROM auth_events WHERE state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?"
    params: list[object] = [now]
    if project_allowlist is not None and "*" not in project_allowlist:
        if project_allowlist:
            placeholders = ",".join("?" for _ in project_allowlist)
            query += f" AND project_id IN ({placeholders})"
            params.extend(sorted(project_allowlist))
        else:
            query += " AND 0"
    rows = conn.execute(query, tuple(params)).fetchall()
    changed = 0
    for row in rows:
        attempts = int(row["attempt_count"])
        if row["kind"] == "launch_requested":
            conn.execute(
                "UPDATE auth_requests SET status = 'pending', claimed_by = NULL, claimed_at = NULL, helper_actor_id = NULL WHERE id = ? AND status = 'claimed' AND helper_actor_id = ?",
                (row["request_id"], row["actor_id"]),
            )
        if attempts >= AUTH_EVENT_MAX_ATTEMPTS:
            cursor = conn.execute(
                "UPDATE auth_events SET state = 'rejected', processed_at = ?, outcome_code = 'retry_exhausted', claimed_by = NULL, claim_expires_at = NULL WHERE id = ? AND state = 'claimed'",
                (now, row["id"]),
            )
            if cursor.rowcount:
                _append_auth_lifecycle(conn, row["request_id"], row["id"], "retry_exhausted", "retry_exhausted", recorded_at=now)
                if row["kind"] == "login_succeeded":
                    verifying = conn.execute(
                        """
                        SELECT event_id FROM auth_lifecycle_events
                        WHERE request_id = ? AND kind = 'verifying'
                        ORDER BY sequence DESC
                        LIMIT 1
                        """,
                        (row["request_id"],),
                    ).fetchone()
                    if verifying is not None and verifying["event_id"] == row["id"]:
                        conn.execute(
                            """
                            UPDATE auth_requests
                               SET status = 'failed', completed_at = ?,
                                   failure_reason = 'verification_failed',
                                   claimed_by = NULL, claimed_at = NULL,
                                   helper_actor_id = NULL
                             WHERE id = ? AND status = 'verifying'
                            """,
                            (now, row["request_id"]),
                        )
                        _append_auth_lifecycle(
                            conn,
                            row["request_id"],
                            row["id"],
                            "failed",
                            "verification_failed",
                            recorded_at=now,
                        )
                changed += cursor.rowcount
            continue
        delay = 2 ** max(0, attempts - 1)
        next_attempt = (now_dt + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = conn.execute(
            "UPDATE auth_events SET state = 'retryable', next_attempt_at = ?, claimed_by = NULL, claim_expires_at = NULL WHERE id = ? AND state = 'claimed'",
            (next_attempt, row["id"]),
        )
        changed += cursor.rowcount
    return changed


def _auth_event_reject(
    conn: sqlite3.Connection, event: sqlite3.Row, *, outcome_code: str
) -> sqlite3.Row:
    now = utcnow()
    conn.execute(
        "UPDATE auth_events SET state = 'rejected', processed_at = ?, outcome_code = ?, claimed_by = NULL, claim_expires_at = NULL WHERE id = ?",
        (now, outcome_code, event["id"]),
    )
    _append_auth_lifecycle(conn, event["request_id"], event["id"], "rejected", outcome_code, recorded_at=now)
    return conn.execute("SELECT * FROM auth_events WHERE id = ?", (event["id"],)).fetchone()


def apply_auth_event_atomic(
    conn: sqlite3.Connection,
    event_id: str,
    dispatcher_id: str,
    *,
    operation: str,
    outcome_code: str | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Execute one Dispatcher-selected operation atomically.

    The Server validates that the selected operation is compatible with the
    immutable event and current request storage state, but never chooses a
    transition or invents a verification result.
    """
    event = conn.execute("SELECT * FROM auth_events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        raise HTTPException(404, "Auth event not found")
    now = utcnow()
    if event["state"] in ("applied", "rejected"):
        request = get_auth_request_or_404(conn, event["request_id"])
        return event, request
    if event["state"] != "claimed" or event["claimed_by"] != dispatcher_id or (
        event["claim_expires_at"] is not None and event["claim_expires_at"] <= now
    ):
        raise HTTPException(409, "Auth event claim is not owned by dispatcher")
    request = get_auth_request_or_404(conn, event["request_id"])
    if request["project_id"] != event["project_id"] or request["auth_ref"] != event["auth_ref"]:
        event = _auth_event_reject(conn, event, outcome_code="request_mismatch")
        return event, request

    kind = event["kind"]
    status = request["status"]
    actor = event["actor_id"]
    bound_actor = request["helper_actor_id"]
    if operation == "reject":
        if outcome_code not in {
            None,
            "invalid_transition",
            "not_request_owner",
            "request_mismatch",
            "retry_exhausted",
            "expired",
        }:
            raise HTTPException(409, "Outcome is not valid for Dispatcher operation")
        event = _auth_event_reject(conn, event, outcome_code=outcome_code or "invalid_transition")
        return event, request

    expected: dict[str, tuple[str, ...]] = {
        "bind_actor": ("launch_requested",),
        "mark_waiting_user": ("browser_opened",),
        "begin_verification": ("login_succeeded",),
        "mark_failed": ("login_failed",),
    }
    if operation not in expected or kind not in expected[operation]:
        raise HTTPException(409, "Dispatcher operation does not match auth event")
    if operation in {"bind_actor", "mark_waiting_user", "begin_verification"} and outcome_code is not None:
        raise HTTPException(409, "Outcome is not valid for Dispatcher operation")
    if operation == "mark_failed" and outcome_code not in {
        None,
        "login_failed",
        "verification_failed",
        "store_unavailable",
        "capture_mismatch",
    }:
        raise HTTPException(409, "Outcome is not valid for Dispatcher operation")
    if operation == "bind_actor":
        if status != "pending" or (bound_actor is not None and bound_actor != actor):
            raise HTTPException(409, "Dispatcher operation is not legal for auth request")
        conn.execute(
            "UPDATE auth_requests SET status = 'claimed', claimed_by = ?, claimed_at = ?, helper_actor_id = ? WHERE id = ?",
            (actor, now, actor, request["id"]),
        )
        _append_auth_lifecycle(conn, request["id"], event_id, "claimed", "claimed", recorded_at=now)
        outcome_code = None
    elif operation == "mark_waiting_user":
        if bound_actor != actor or status != "claimed":
            raise HTTPException(409, "Dispatcher operation is not legal for auth request")
        conn.execute("UPDATE auth_requests SET status = 'waiting_user' WHERE id = ?", (request["id"],))
        _append_auth_lifecycle(conn, request["id"], event_id, "waiting_user", "waiting_user", recorded_at=now)
        outcome_code = None
    elif operation == "begin_verification":
        if bound_actor != actor or status not in {"waiting_user", "verifying"}:
            raise HTTPException(409, "Dispatcher operation is not legal for auth request")
        if status == "verifying":
            verifying = conn.execute(
                """
                SELECT event_id FROM auth_lifecycle_events
                WHERE request_id = ? AND kind = 'verifying'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (request["id"],),
            ).fetchone()
            if verifying is None or verifying["event_id"] != event_id:
                event = _auth_event_reject(conn, event, outcome_code="invalid_transition")
                return event, request
        if status == "waiting_user":
            conn.execute("UPDATE auth_requests SET status = 'verifying' WHERE id = ?", (request["id"],))
            _append_auth_lifecycle(conn, request["id"], event_id, "verifying", "verifying", recorded_at=now)
        # The graph effect is durable before the Dispatcher opens the secret store.
        # Keep both the event claim and request in verifying for crash recovery.
        event_after = conn.execute("SELECT * FROM auth_events WHERE id = ?", (event_id,)).fetchone()
        ensure_auth_graph_outbox(conn, event_after, now=now)
        return (
            event_after,
            conn.execute("SELECT * FROM auth_requests WHERE id = ?", (request["id"],)).fetchone(),
        )
    elif operation == "mark_failed":
        if bound_actor != actor or status not in {"claimed", "waiting_user", "verifying"}:
            raise HTTPException(409, "Dispatcher operation is not legal for auth request")
        outcome_code = outcome_code or "login_failed"
        conn.execute(
            "UPDATE auth_requests SET status = 'verifying', failure_reason = NULL WHERE id = ?",
            (request["id"],),
        )
        _append_auth_lifecycle(conn, request["id"], event_id, "verifying", outcome_code, recorded_at=now)
        event_after = conn.execute("SELECT * FROM auth_events WHERE id = ?", (event_id,)).fetchone()
        ensure_auth_graph_outbox(conn, event_after, now=now)
        return (
            event_after,
            conn.execute("SELECT * FROM auth_requests WHERE id = ?", (request["id"],)).fetchone(),
        )

    conn.execute(
        "UPDATE auth_events SET state = 'applied', processed_at = ?, outcome_code = ?, claimed_by = NULL, claim_expires_at = NULL WHERE id = ?",
        (now, outcome_code or status, event_id),
    )
    return (
        conn.execute("SELECT * FROM auth_events WHERE id = ?", (event_id,)).fetchone(),
        conn.execute("SELECT * FROM auth_requests WHERE id = ?", (request["id"],)).fetchone(),
    )


def expire_due_auth_requests(
    conn: sqlite3.Connection, *, project_allowlist: frozenset[str] | None = None
) -> int:
    """Expire due nonterminal requests using the persisted Server TTL."""
    now = utcnow()
    query = "SELECT * FROM auth_requests WHERE status IN ('pending','claimed','waiting_user','verifying') AND expires_at IS NOT NULL AND expires_at <= ?"
    params: list[object] = [now]
    if project_allowlist is not None and "*" not in project_allowlist:
        if project_allowlist:
            placeholders = ",".join("?" for _ in project_allowlist)
            query += f" AND project_id IN ({placeholders})"
            params.extend(sorted(project_allowlist))
        else:
            query += " AND 0"
    rows = conn.execute(query, tuple(params)).fetchall()
    for row in rows:
        conn.execute("UPDATE auth_requests SET status = 'expired', completed_at = ? WHERE id = ? AND status IN ('pending','claimed','waiting_user','verifying')", (now, row["id"]))
        _append_auth_lifecycle(conn, row["id"], f"ttl:{row['id']}:{row['expiry_generation']}", "expired", "expired", recorded_at=now)
    return len(rows)


def get_auth_claim_ttl(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT auth_claim_ttl FROM settings WHERE rowid = 1").fetchone()
    return row["auth_claim_ttl"]


def get_auth_request_ttl(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT auth_request_ttl FROM settings WHERE rowid = 1").fetchone()
    return row["auth_request_ttl"]


def auth_request_expiry(created_at: str, ttl: int) -> str | None:
    if ttl <= 0:
        return None
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=ttl)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_auth_request_id(conn: sqlite3.Connection) -> str:
    """Return the next global auth request id (``auth_001`` ...)."""
    conn.execute(
        "INSERT OR IGNORE INTO counters (name, value) VALUES ('auth_request', 0)"
    )
    conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = 'auth_request'"
    )
    row = conn.execute(
        "SELECT value FROM counters WHERE name = 'auth_request'"
    ).fetchone()
    assert row is not None
    return f"auth_{row['value']:03d}"


def find_active_auth_request(
    conn: sqlite3.Connection, project_id: str, auth_ref: str
) -> sqlite3.Row | None:
    """Return an in-flight auth request for the logical key, if any.

    The logical dedup key is ``(project_id, auth_ref, active-status)`` so Reason never
    creates a duplicate request while one is still being handled.
    """
    return conn.execute(
        """
        SELECT * FROM auth_requests
        WHERE project_id = ?
          AND auth_ref = ?
          AND status IN ('pending', 'claimed', 'waiting_user', 'verifying')
        """,
        (project_id, auth_ref),
    ).fetchone()


def get_auth_request_or_404(
    conn: sqlite3.Connection, request_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM auth_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Auth request not found")
    return row


def auth_request_to_model(row: sqlite3.Row) -> "AuthRequest":
    from cairn.server.models import AuthRequest

    return AuthRequest(
        id=row["id"],
        project_id=row["project_id"],
        source_fact_ids=_split_source_fact_ids(row["source_fact_ids"]),
        auth_ref=row["auth_ref"],
        role=row["role"],
        login_url=row["login_url"],
        reason=row["reason"],
        status=row["status"],
        claimed_by=row["claimed_by"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        failure_reason=row["failure_reason"],
        helper_actor_id=row["helper_actor_id"],
        expires_at=row["expires_at"],
        expiry_generation=row["expiry_generation"],
    )


def _split_source_fact_ids(raw: str) -> list[str]:
    if not raw:
        return []
    return [part for part in raw.split("\n") if part]


def _join_source_fact_ids(fact_ids: list[str]) -> str:
    return "\n".join(fact_ids)


def create_auth_graph_intent(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_key: str,
    source_fact_ids: list[str],
    description: str,
    creator: str = "operator.auth",
    worker: str = "operator.auth",
) -> dict[str, object]:
    """Create or retrieve an auth intent by its durable source key."""
    if not (source_key.startswith("auth:") or source_key.startswith("auth-event:")):
        raise HTTPException(422, "auth graph source key is required")
    check_project_active(conn, project_id)
    validate_facts_exist(conn, project_id, source_fact_ids)
    existing = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND source_key = ?",
        (project_id, source_key),
    ).fetchone()
    if existing is not None:
        existing_sources = [row["fact_id"] for row in conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (existing["id"], project_id),
        ).fetchall()]
        if existing["description"] != description or existing_sources != source_fact_ids:
            raise HTTPException(409, "graph source key conflict")
        model = intent_to_model(conn, existing, project_id).model_dump(by_alias=True)
        model["source_key"] = source_key
        conn.execute(
            "UPDATE auth_graph_outbox SET state = CASE WHEN state = 'pending' THEN 'intent_created' ELSE state END, intent_id = COALESCE(intent_id, ?), updated_at = ? WHERE project_id = ? AND intent_source_key = ?",
            (existing["id"], utcnow(), project_id, source_key),
        )
        return model
    validate_intent_creator_worker(creator, worker)
    now = utcnow()
    intent_id = next_intent_id(conn, project_id)
    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at, source_key) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?)",
        (intent_id, project_id, description, creator, worker, now, now, source_key),
    )
    for fact_id in source_fact_ids:
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
            (intent_id, project_id, fact_id),
        )
    row = conn.execute("SELECT * FROM intents WHERE id = ? AND project_id = ?", (intent_id, project_id)).fetchone()
    model = intent_to_model(conn, row, project_id).model_dump(by_alias=True)
    model["source_key"] = source_key
    conn.execute(
        "UPDATE auth_graph_outbox SET state = CASE WHEN state = 'pending' THEN 'intent_created' ELSE state END, intent_id = ?, updated_at = ? WHERE project_id = ? AND intent_source_key = ?",
        (intent_id, now, project_id, source_key),
    )
    return model


def conclude_auth_graph_intent(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    intent_source_key: str,
    fact_source_key: str,
    worker: str,
    description: str,
) -> dict[str, object]:
    """Conclude a source-keyed auth intent exactly once."""
    if not (intent_source_key.startswith("auth:") or intent_source_key.startswith("auth-event:")) or not (
        fact_source_key.startswith("auth:") or fact_source_key.startswith("auth-event:")
    ):
        raise HTTPException(422, "auth graph source key is required")
    check_project_active(conn, project_id)
    intent = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND source_key = ?",
        (project_id, intent_source_key),
    ).fetchone()
    if intent is None:
        raise HTTPException(404, "Auth graph intent not found")
    if intent["to_fact_id"] is not None:
        fact = conn.execute(
            "SELECT * FROM facts WHERE project_id = ? AND id = ?", (project_id, intent["to_fact_id"])
        ).fetchone()
        if fact is None or fact["source_key"] != fact_source_key:
            raise HTTPException(409, "graph conclusion source key conflict")
        result = {"intent_id": intent["id"], "fact_id": fact["id"], "fact": {"id": fact["id"], "description": fact["description"]}}
        conn.execute(
            "UPDATE auth_graph_outbox SET state = CASE WHEN state IN ('pending', 'intent_created') THEN 'fact_created' ELSE state END, intent_id = COALESCE(intent_id, ?), fact_id = COALESCE(fact_id, ?), updated_at = ? WHERE project_id = ? AND intent_source_key = ?",
            (intent["id"], fact["id"], utcnow(), project_id, intent_source_key),
        )
        return result
    existing_fact = conn.execute(
        "SELECT * FROM facts WHERE project_id = ? AND source_key = ?", (project_id, fact_source_key)
    ).fetchone()
    now = utcnow()
    if existing_fact is None:
        fact_id = next_fact_id(conn, project_id)
        conn.execute(
            "INSERT INTO facts (id, project_id, description, source_key) VALUES (?, ?, ?, ?)",
            (fact_id, project_id, description, fact_source_key),
        )
        existing_fact = conn.execute("SELECT * FROM facts WHERE id = ? AND project_id = ?", (fact_id, project_id)).fetchone()
    elif existing_fact["description"] != description:
        raise HTTPException(409, "graph source key conflict")
    conn.execute(
        "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ? AND to_fact_id IS NULL",
        (existing_fact["id"], worker, now, now, intent["id"], project_id),
    )
    conn.execute(
        "UPDATE auth_graph_outbox SET state = CASE WHEN state IN ('pending', 'intent_created') THEN 'fact_created' ELSE state END, intent_id = ?, fact_id = ?, updated_at = ? WHERE project_id = ? AND intent_source_key = ?",
        (intent["id"], existing_fact["id"], now, project_id, intent_source_key),
    )
    return {"intent_id": intent["id"], "fact_id": existing_fact["id"], "fact": {"id": existing_fact["id"], "description": existing_fact["description"]}}


def claim_auth_request_atomic(
    conn: sqlite3.Connection, request_id: str, helper_id: str
) -> bool:
    """Atomically claim a ``pending`` auth request.

    Returns ``True`` only if exactly one row was transitioned. A ``0`` rowcount means
    another helper already claimed it (or it is no longer pending), so the caller must
    back off to avoid duplicate popups across multiple helpers / dispatchers.
    """
    now = utcnow()
    cursor = conn.execute(
        """
        UPDATE auth_requests
        SET status = 'claimed',
            claimed_by = ?,
            claimed_at = ?
        WHERE id = ?
          AND status = 'pending'
        """,
        (helper_id, now, request_id),
    )
    return cursor.rowcount == 1


class AuthStateResolver:
    """Resolve the current authentication state for ``(project_id, auth_ref)``.

    Authentication state is a *temporal* fact: a later ``AuthSessionInvalid`` fact
    overrides an earlier ``AuthSessionVerified`` fact, and vice-versa. This resolver
    inspects the fact descriptions in insertion order and returns the latest state.
    """

    VERIFIED_MARKER = "AuthSessionVerified"
    INVALID_MARKER = "AuthSessionInvalid"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def resolve(self, project_id: str, auth_ref: str) -> str:
        rows = self._conn.execute(
            "SELECT description FROM facts WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        state = "missing"
        for row in rows:
            description = row["description"] or ""
            if auth_ref not in description:
                continue
            if self.VERIFIED_MARKER in description:
                state = "valid"
            elif self.INVALID_MARKER in description:
                state = "invalid"
        return state


def build_auth_state_resolver(conn: sqlite3.Connection) -> AuthStateResolver:
    return AuthStateResolver(conn)


def _parse_ts(value: str | None) -> datetime:
    """Parse a stored ``YYYY-MM-DDTHH:MM:SSZ`` timestamp to a tz-aware datetime."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _age_seconds(value: str | None, now: datetime) -> float:
    return (now - _parse_ts(value)).total_seconds()


def expire_stale_claims(conn: sqlite3.Connection, claim_ttl: int) -> int:
    """Return ``claimed`` auth requests stuck past ``claim_ttl`` seconds to ``pending``.

    A helper claims a request, then immediately opens the browser and transitions it to
    ``waiting_user``. If the helper crashes (or the operator never acts) before that
    transition, the request must not stay ``claimed`` forever, otherwise the dedup guard
    in :func:`find_active_auth_request` would block re-claiming forever. This releases
    the stale claim so another helper can pick it up.

    Returns the number of rows released.
    """
    if claim_ttl <= 0:
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute(
        """
        UPDATE auth_requests
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at = NULL
        WHERE status = 'claimed'
          AND claimed_at IS NOT NULL
          AND (julianday(?) - julianday(claimed_at)) * 86400 > ?
        """,
        (cutoff, claim_ttl),
    )
    return cursor.rowcount


def expire_stale_requests(conn: sqlite3.Connection, request_ttl: int) -> int:
    """Mark active auth requests older than ``request_ttl`` seconds as ``expired``.

    Applies to ``pending`` / ``claimed`` / ``waiting_user`` / ``verifying``. Expired
    requests no longer participate in dedup (they are not in the active status set), so
    a later Reason run may create a fresh request.
    """
    if request_ttl <= 0:
        return 0
    # Rows created before the expiry column was introduced have a null value;
    # initialize those deterministically before using the same lifecycle-aware
    # reaper as the Dispatcher internal endpoint.
    rows = conn.execute(
        "SELECT id, created_at FROM auth_requests WHERE status IN ('pending','claimed','waiting_user','verifying') AND expires_at IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE auth_requests SET expires_at = ? WHERE id = ? AND expires_at IS NULL",
            (auth_request_expiry(row["created_at"], request_ttl), row["id"]),
        )
    return expire_due_auth_requests(conn)

