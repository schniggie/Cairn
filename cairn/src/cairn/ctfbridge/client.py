"""Minimal HTTP client for the Cairn server, used by the bridge.

Only the endpoints the bridge needs are wrapped here. All methods raise
:class:`BridgeError` on non-2xx responses.
"""

from __future__ import annotations

from typing import Any

import requests


class BridgeError(RuntimeError):
    """Raised when a Cairn API call fails."""


class CairnApi:
    def __init__(self, server_url: str, timeout: float = 15.0, session: requests.Session | None = None) -> None:
        self.base = server_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BridgeError(f"request to {path} failed: {exc}") from exc
        if resp.status_code in (200, 201):
            return resp.json() if resp.content else None
        if resp.status_code == 204:
            return None
        detail = ""
        try:
            payload = resp.json()
            detail = payload.get("detail") if isinstance(payload, dict) else ""
        except ValueError:
            detail = resp.text[:300]
        raise BridgeError(f"HTTP {resp.status_code} on {path}: {detail or 'no detail'}")

    # ------------------------------------------------------------- CTF config

    def get_config(self, *, full: bool = False) -> dict:
        return self._request("GET", "/ctf/config", params={"full": "true" if full else ""})

    def set_mode(self, mode: str) -> dict:
        return self._request("PUT", "/ctf/mode", body={"mode": mode})

    def get_status(self) -> dict:
        return self._request("GET", "/ctf/status")

    def request_sync(self) -> dict:
        return self._request("POST", "/ctf/sync", body={})

    def ack_sync(self, *, synced_at: str | None = None) -> dict:
        body = {"last_sync_at": synced_at} if synced_at else {}
        return self._request("POST", "/ctf/sync/ack", body=body)

    def heartbeat(self, *, error: str | None = None, model_ok: bool | None = None, model_error: str | None = None) -> dict:
        body: dict = {"error": error}
        if model_ok is not None:
            body["model_ok"] = model_ok
        if model_error is not None:
            body["model_error"] = model_error
        return self._request("POST", "/ctf/heartbeat", body=body)

    # ------------------------------------------------------------ challenges

    def list_challenges(self) -> list[dict]:
        return self._request("GET", "/ctf/challenges")

    def create_challenge(self, **fields) -> dict:
        return self._request("POST", "/ctf/challenges", body=fields)

    def update_challenge(self, challenge_id: int | str, **fields) -> dict:
        return self._request("PUT", f"/ctf/challenges/{challenge_id}", body=fields)

    def retry_challenge(self, challenge_id: int | str) -> dict:
        return self._request("POST", f"/ctf/challenges/{challenge_id}/retry", body={})

    def pause_challenge(self, challenge_id: int | str) -> dict:
        return self._request("POST", f"/ctf/challenges/{challenge_id}/pause", body={})

    def resume_challenge(self, challenge_id: int | str) -> dict:
        return self._request("POST", f"/ctf/challenges/{challenge_id}/resume", body={})

    def stop_challenge(self, challenge_id: int | str) -> dict:
        return self._request("POST", f"/ctf/challenges/{challenge_id}/stop", body={})

    # --------------------------------------------------------------- projects

    def create_project(self, *, title: str, origin: str, goal: str, hints: list[dict] | None = None) -> dict:
        return self._request(
            "POST",
            "/projects",
            body={"title": title, "origin": origin, "goal": goal, "hints": hints or []},
        )

    def get_project(self, project_id: str) -> dict:
        return self._request("GET", f"/projects/{project_id}")

    def update_project_status(self, project_id: str, status: str) -> dict:
        return self._request("PUT", f"/projects/{project_id}/status", body={"status": status})

    def reopen_project(self, project_id: str, description: str) -> dict:
        return self._request(
            "POST",
            f"/projects/{project_id}/reopen",
            body={"description": description, "creator": "ctf-bridge"},
        )
