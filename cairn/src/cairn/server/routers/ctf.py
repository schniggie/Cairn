"""CTF configuration and challenge-state API.

Platform interaction itself lives in the bridge process; the server only
persists config / challenge rows and answers queries. The two exceptions are
``/ctf/test`` and ``/ctf/submit``, which call the adapter directly so the UI
can probe connectivity and submit a flag without the bridge running.
"""

from __future__ import annotations

import time

import requests
from fastapi import APIRouter, HTTPException

from cairn.ctfbridge.adapters import get_adapter
from cairn.server import ctf_service
from cairn.server.db import get_conn
from cairn.server.models import (
    CtfChallenge,
    CtfConfig,
    CtfConfigUpdate,
    CtfHeartbeatRequest,
    CtfModeRequest,
    CtfSubmitRequest,
    CtfTestResult,
)

router = APIRouter(prefix="/ctf", tags=["ctf"])


def _adapter_from_config() -> tuple[str, dict, object]:
    with get_conn() as conn:
        cfg = ctf_service.load_config(conn, full=True)
    source = get_adapter(
        cfg["adapter"],
        base_url=cfg["base_url"],
        token=cfg["token"],
        team_name=cfg["team_name"],
        env_poll_interval=cfg.get("env_poll_interval") or 5,
        env_timeout=cfg.get("env_timeout") or 180,
    )
    return cfg["adapter"], cfg, source


@router.get("/config", response_model=CtfConfig)
def get_config(full: bool = False):
    with get_conn() as conn:
        return ctf_service.load_config(conn, full=full)


@router.put("/config", response_model=CtfConfig)
def put_config(body: CtfConfigUpdate):
    with get_conn() as conn:
        return ctf_service.mask_config(ctf_service.update_config(conn, body.model_dump(exclude_none=True)))


@router.put("/mode", response_model=CtfConfig)
def put_mode(body: CtfModeRequest):
    with get_conn() as conn:
        return ctf_service.mask_config(ctf_service.set_mode(conn, body.mode.value))


@router.get("/challenges", response_model=list[CtfChallenge])
def get_challenges():
    with get_conn() as conn:
        return ctf_service.list_challenges(conn)


@router.post("/challenges", response_model=CtfChallenge, status_code=201)
def post_challenge(body: dict):
    with get_conn() as conn:
        return ctf_service.create_challenge(conn, body)


@router.put("/challenges/{challenge_id}", response_model=CtfChallenge)
def put_challenge(challenge_id: int, body: dict):
    with get_conn() as conn:
        return ctf_service.update_challenge(conn, challenge_id, body)


# --------------------------------------------------------- challenge lifecycle


def _stop_project(project_id: str) -> None:
    """Stop a Cairn project so it stops burning LLM calls.

    Mirrors the projects router's stop path (status flip); reason leases are
    already expired by the scheduler once the project is not active.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET status = 'stopped' WHERE id = ? AND status IN ('active', 'stopped')",
            (project_id,),
        )


@router.post("/challenges/{challenge_id}/retry", response_model=CtfChallenge)
def retry_challenge(challenge_id: int):
    """Re-queue a failed / paused / stuck challenge so the bridge builds it fresh."""
    with get_conn() as conn:
        return ctf_service.retry_challenge(conn, challenge_id)


@router.post("/challenges/{challenge_id}/pause", response_model=CtfChallenge)
def pause_challenge(challenge_id: int):
    """Hold a challenge and stop its project without failing it."""
    with get_conn() as conn:
        challenge, project_id = ctf_service.pause_challenge(conn, challenge_id)
    if project_id:
        _stop_project(project_id)
    return challenge


@router.post("/challenges/{challenge_id}/resume", response_model=CtfChallenge)
def resume_challenge(challenge_id: int):
    """Send a paused challenge back to the queue."""
    with get_conn() as conn:
        return ctf_service.resume_challenge(conn, challenge_id)


@router.post("/challenges/{challenge_id}/stop", response_model=CtfChallenge)
def stop_challenge(challenge_id: int):
    """Permanently stop a challenge (marked failed; retryable later)."""
    with get_conn() as conn:
        challenge, project_id = ctf_service.stop_challenge(conn, challenge_id)
    if project_id:
        _stop_project(project_id)
    return challenge


_OVERVIEW_CACHE: dict = {"at": 0.0, "data": None}
_OVERVIEW_TTL = 30.0


@router.get("/status")
def get_status():
    with get_conn() as conn:
        payload = ctf_service.status_payload(conn)
    # Merge the platform score/rank (cached, so polling /ctf/status does not
    # hit the platform on every request).
    now = time.monotonic()
    if now - _OVERVIEW_CACHE["at"] > _OVERVIEW_TTL:
        try:
            _, _, source = _adapter_from_config()
            overview = source.overview()
            _OVERVIEW_CACHE.update({"at": now, "data": overview})
        except Exception:  # noqa: BLE001 - status must stay available
            _OVERVIEW_CACHE.update({"at": now, "data": None})
    if _OVERVIEW_CACHE["data"]:
        payload.update(_OVERVIEW_CACHE["data"])
    return payload


@router.post("/test", response_model=CtfTestResult)
def test_connection():
    _, _, source = _adapter_from_config()
    try:
        source.verify_connection()
    except Exception as exc:  # noqa: BLE001 - report the probe result
        return CtfTestResult(ok=False, detail=str(exc))
    return CtfTestResult(ok=True, detail="connection ok")


@router.post("/test-model", response_model=CtfTestResult)
def test_model_connection():
    """Probe the configured LLM endpoint (Anthropic-compatible /v1/messages).

    Mirrors the bridge's model health check so the UI can verify the model
    config without the bridge running.
    """
    with get_conn() as conn:
        cfg = ctf_service.load_config(conn, full=True)
    base_url = (cfg.get("model_base_url") or "").rstrip("/")
    if not base_url or not cfg.get("model_name") or not cfg.get("model_api_key"):
        return CtfTestResult(ok=False, detail="model 未配置（base_url / model / api_key）")
    try:
        resp = requests.post(
            f"{base_url}/v1/messages",
            json={
                "model": cfg["model_name"],
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            headers={
                "Authorization": f"Bearer {cfg['model_api_key']}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message") or f"HTTP {resp.status_code}"
            except ValueError:
                detail = f"HTTP {resp.status_code}"
            return CtfTestResult(ok=False, detail=str(detail))
        return CtfTestResult(ok=True, detail="model ok")
    except Exception as exc:  # noqa: BLE001
        return CtfTestResult(ok=False, detail=str(exc))


@router.post("/sync")
def request_sync():
    with get_conn() as conn:
        return ctf_service.mask_config(ctf_service.request_sync(conn))


@router.post("/sync/ack")
def ack_sync(body: dict | None = None):
    body = body or {}
    synced_at = body.get("last_sync_at")
    with get_conn() as conn:
        if synced_at:
            ctf_service.set_last_sync_at(conn, str(synced_at))
        else:
            ctf_service.request_sync_ack(conn)
        return ctf_service.load_config(conn, full=False)


@router.post("/heartbeat")
def heartbeat(body: CtfHeartbeatRequest):
    with get_conn() as conn:
        return ctf_service.mask_config(
            ctf_service.set_bridge_heartbeat(
                conn,
                error=body.error,
                model_ok=body.model_ok,
                model_error=body.model_error,
            )
        )


@router.post("/submit")
def submit_flag(body: CtfSubmitRequest):
    with get_conn() as conn:
        row = _resolve_challenge_row(conn, body.challenge_id)
        external_id = row["external_id"]

    _, _, source = _adapter_from_config()
    try:
        result, message = source.submit_flag(external_id, body.flag)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc), "status": "failed"}

    result_to_status = {
        "success": "solved",
        "wrong_flag": "wrong_flag",
        "rate_limited": "rate_limited",
        "error": "failed",
    }
    status = result_to_status[result.value]
    with get_conn() as conn:
        updated = ctf_service.update_challenge(
            conn,
            row["id"],
            {
                "status": status,
                "last_flag": body.flag,
                "attempt_count": row["attempt_count"] + 1,
            },
        )
    return {"ok": result.value == "success", "detail": message, "status": updated.status}


def _resolve_challenge_row(conn, challenge_id: str):
    try:
        return ctf_service.get_challenge_or_404(conn, int(challenge_id))
    except ValueError:
        pass
    row = conn.execute(
        "SELECT * FROM ctf_challenges WHERE external_id = ?", (challenge_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Challenge not found")
    return row
