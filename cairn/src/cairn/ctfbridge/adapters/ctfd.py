"""CTFd REST v1 reference adapter.

Covers the de-facto CTFd API::

    GET  {base}/api/v1/challenges            -> list
    GET  {base}/api/v1/challenges/{id}       -> detail (incl. instance for
                                                dynamic-target deployments)
    POST {base}/api/v1/challenges/{id}/attempts  body {"attempt": flag}

Auth uses a ``Token <token>`` header, which is the CTFd default. Some
deployments use cookies/sessions or a different header — adjust
:meth:`CtfdSource.verify_connection` to probe the real platform (see the
development plan, section 7).
"""

from __future__ import annotations

import requests

from cairn.ctfbridge.adapters.base import Challenge, ChallengeSource, SubmissionResult


class CtfdSource(ChallengeSource):
    name = "ctfd"

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        team_name: str = "",
        timeout: float = 15.0,
        **kwargs,  # tolerate adapter-specific options from ctf_config
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.team_name = team_name
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {token}" if token else "",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------ utils

    def _get(self, path: str):
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("data")

    def _post(self, path: str, body: dict):
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.post(url, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"request failed: {exc}") from exc
        return resp

    # ----------------------------------------------------------- ChallengeSource

    def verify_connection(self) -> bool:
        if not self.base_url:
            raise RuntimeError("base_url is not configured")
        self._get("/api/v1/challenges")
        return True

    def list_challenges(self) -> list[Challenge]:
        data = self._get("/api/v1/challenges") or []
        challenges: list[Challenge] = []
        for item in data:
            challenges.append(
                Challenge(
                    external_id=str(item.get("id", "")),
                    title=str(item.get("name") or "untitled"),
                    category=str(item.get("category") or ""),
                    points=int(item.get("value") or 0),
                )
            )
        return challenges

    def get_challenge(self, external_id: str) -> Challenge:
        data = self._get(f"/api/v1/challenges/{external_id}") or {}

        description = str(data.get("description") or "")

        attachments = []
        for f in data.get("files", []) or []:
            attachments.append(f"{self.base_url}/files/{f}")

        # Per-team instance: many dynamic-target platforms expose host/port
        # either in an "instance" object or a "target" string. Exact shape is
        # platform-specific — see plan section 7.3.
        target = ""
        instance = data.get("instance")
        if isinstance(instance, dict):
            host = instance.get("host") or instance.get("hostname") or ""
            port = instance.get("port")
            if host:
                target = f"{host}:{port}" if port else str(host)
        if not target and data.get("target"):
            target = str(data["target"])

        hints = []
        for h in data.get("hints", []) or []:
            # CTFd list items are hint ids; full text comes from the hints API.
            if isinstance(h, dict):
                content = h.get("content") or h.get("hint") or ""
                if content:
                    hints.append(str(content))

        return Challenge(
            external_id=external_id,
            title=str(data.get("name") or external_id),
            category=str(data.get("category") or ""),
            points=int(data.get("value") or 0),
            description=description,
            target=target,
            attachments=attachments,
            hints=hints,
        )

    def submit_flag(self, external_id: str, flag: str) -> tuple[SubmissionResult, str]:
        resp = self._post(
            f"/api/v1/challenges/{external_id}/attempts",
            {"attempt": flag},
        )
        status = resp.status_code
        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if status in (200, 201):
            data = payload.get("data") or {}
            if data.get("status") in ("correct", "success") or payload.get("success"):
                return SubmissionResult.SUCCESS, "flag accepted"
            if data.get("status") == "incorrect":
                return SubmissionResult.WRONG_FLAG, "flag rejected"
            message = str(payload.get("message") or data.get("message") or "unknown result")
            return SubmissionResult.ERROR, message
        if status == 429:
            return SubmissionResult.RATE_LIMITED, "submission rate limited"
        if status in (400, 401, 403):
            # 401/403 can mean a wrong flag, and some deployments reject the
            # submission with a non-2xx status.
            return SubmissionResult.WRONG_FLAG, f"HTTP {status}"
        return SubmissionResult.ERROR, f"HTTP {status}"
