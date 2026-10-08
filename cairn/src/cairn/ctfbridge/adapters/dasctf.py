"""DasCTF platform adapter (第九届西湖论剑 / gcsis.dasctf.com).

Implements the ``/slab-match/api/v1/agent`` REST API::

    GET  {base}/slab-match/api/v1/agent/ctf/exercise-list        -> categories+challenges
    GET  {base}/slab-match/api/v1/agent/ctf/exercise?exerciseId= -> full detail
    POST {base}/slab-match/api/v1/agent/ctf/build-exercise-env   -> spawn target
    POST {base}/slab-match/api/v1/agent/answer-panel/answer      -> submit flag

Auth uses a single ``X-Agent-AccessKey`` header. All responses are wrapped as
``{"code", "message", "data"}``; ``code == "00000"`` is success.

Platform specifics this adapter encodes (verified against the 2026-08-17 test
competition):

* ``exercise-list`` returns categories, each with a ``corpus`` of challenges —
  the challenge ``id`` inside ``corpus`` is the ``exerciseId`` used everywhere.
* ``exercise`` detail is *stale* until the per-team target is provisioned: when
  ``isNeedInit=true`` the ``endpoints`` array is empty and you must call
  ``build-exercise-env`` then poll the detail until ``isNeedCheck=false``.
* ``attachment`` is either a single object ``{url, name, ...}`` or (per the
  published doc) ``{files: [{url, ...}]}`` — both shapes are handled.
* Wrong flags return ``code=40001`` with a message that includes the number of
  submissions remaining (50 per challenge). Do **not** brute force.
* Flags follow the ``DASCTF{...}`` format.
"""

from __future__ import annotations

import logging
import re
import time

import requests

from cairn.ctfbridge.adapters.base import Challenge, ChallengeSource, SubmissionResult

log = logging.getLogger(__name__)

_AGENT_PATH = "/slab-match/api/v1/agent"

# Submit endpoints reject with one of these messages; heuristics only — the
# authoritative "wrong flag" signal is code 40001 / a message mentioning 错误.
_WRONG_FLAG_HINTS = ("flag错误", "答案错误", "提交flag错误", "不正确")
_RATE_LIMIT_HINTS = ("频繁", "限流", "冷却", "太快", "频率", "稍后再试")

# Per the competition manual, flags are DASCTF{...} / flag{...} but submission
# takes only the content inside the braces. Strip the known wrappers.
_FLAG_WRAPPER = re.compile(r"^(?:DASCTF|flag|ctf|ctfshow)\{([^}]*)\}$")


def _is_success(payload: dict) -> bool:
    return payload.get("code") == "00000"


class DasctfSource(ChallengeSource):
    name = "dasctf"

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        team_name: str = "",
        timeout: float = 20.0,
        env_poll_interval: float = 5.0,
        env_timeout: float = 180.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.team_name = team_name
        self.timeout = timeout
        self.env_poll_interval = env_poll_interval
        self.env_timeout = env_timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Agent-AccessKey": token,
                "Content-Type": "application/json",
                # The platform's WAF blocks the default python-requests UA
                # (403), while a browser UA passes. curl works unmodified.
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )

    # ------------------------------------------------------------------ utils

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{_AGENT_PATH}{path}"
        try:
            resp = self.session.request(method, url, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"invalid JSON from platform: {resp.text[:300]}") from exc
        return payload

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body)

    # ----------------------------------------------------------- ChallengeSource

    def verify_connection(self) -> bool:
        if not self.base_url:
            raise RuntimeError("base_url is not configured")
        if not self.token:
            raise RuntimeError("X-Agent-AccessKey (token) is not configured")
        payload = self._get("/match/notice/match-info")
        if not _is_success(payload):
            raise RuntimeError(payload.get("message") or f"unexpected response: {payload}")
        return True

    def overview(self) -> dict | None:
        """Fetch the platform score/rank; ``None`` when unavailable."""
        try:
            payload = self._get("/answer-panel/overview")
        except RuntimeError:
            return None
        if not _is_success(payload):
            return None
        data = payload.get("data") or {}
        result: dict = {}
        if data.get("stagePoint") is not None:
            result["platform_score"] = data["stagePoint"]
        if data.get("stageRank") is not None:
            result["platform_rank"] = data["stageRank"]
        return result or None

    def list_challenges(self) -> list[Challenge]:
        payload = self._get("/ctf/exercise-list")
        if not _is_success(payload):
            raise RuntimeError(payload.get("message") or f"unexpected response: {payload}")
        challenges: list[Challenge] = []
        for category in payload.get("data") or []:
            cat_name = str(category.get("name") or "")
            for item in category.get("corpus") or []:
                # Only open, unsolved challenges. Solved ones would just burn
                # LLM calls re-solving an already-submitted flag.
                if not item.get("isOpen", True):
                    continue
                if item.get("hasSolved"):
                    continue
                challenges.append(
                    Challenge(
                        external_id=str(item.get("id", "")),
                        title=str(item.get("name") or "untitled"),
                        category=cat_name,
                    )
                )
        return challenges

    def get_challenge(self, external_id: str) -> Challenge:
        data = self._fetch_detail(external_id)
        data = self._ensure_target_ready(external_id, data)

        points = 0
        try:
            points = int(float(data.get("score") or 0))
        except (TypeError, ValueError):
            pass

        return Challenge(
            external_id=external_id,
            title=str(data.get("name") or external_id),
            category="",
            points=points,
            description=str(data.get("description") or ""),
            target=self._compose_target(data.get("endpoints")),
            attachments=_collect_attachments(data.get("attachment")),
        )

    def recover_env(self, external_id: str) -> None:
        """Release the per-team target instance for a solved/stopped challenge.

        DasCTF caps each team at 3 concurrent target instances; leaving solved
        challenges running blocks new ones (``40409 已达数量``). Recovery is
        idempotent — destroying an already-destroyed instance is harmless.
        """
        payload = self._post("/ctf/recover-exercise-env", {"exerciseId": int(external_id)})
        if not _is_success(payload):
            # "非独占场景，无权访问" (no exclusive env) is not an error worth
            # surfacing; everything else is.
            message = payload.get("message") or ""
            if "非独占" not in message and "销毁" not in message:
                log.warning("recover env for %s returned: %s", external_id, message)

    def submit_flag(self, external_id: str, flag: str) -> tuple[SubmissionResult, str]:
        # Manual rule: submit only the content inside DASCTF{...} / flag{...}.
        match = _FLAG_WRAPPER.match(flag.strip())
        body = {"exerciseId": int(external_id), "flag": match.group(1) if match else flag.strip()}
        try:
            payload = self._request("POST", "/answer-panel/answer", body)
        except RuntimeError as exc:
            text = str(exc)
            if "429" in text:
                return SubmissionResult.RATE_LIMITED, "submission rate limited"
            return SubmissionResult.ERROR, text

        message = str(payload.get("message") or "")
        if _is_success(payload):
            data = payload.get("data") or {}
            if data.get("isCorrect") is True:
                return SubmissionResult.SUCCESS, "flag accepted"
            # code 00000 without isCorrect — treat as unknown, not a wrong flag.
            return SubmissionResult.ERROR, message or "unexpected success response"

        # Wrong flag: code 40001 / message mentions the flag was rejected. The
        # message includes the remaining attempt count, safe to log.
        if payload.get("code") == "40001" or any(h in message for h in _WRONG_FLAG_HINTS):
            return SubmissionResult.WRONG_FLAG, message or "flag rejected"
        if any(h in message for h in _RATE_LIMIT_HINTS):
            return SubmissionResult.RATE_LIMITED, message
        return SubmissionResult.ERROR, message or f"code={payload.get('code')}"

    # ------------------------------------------------------------------- helpers

    def _fetch_detail(self, external_id: str) -> dict:
        payload = self._get(f"/ctf/exercise?exerciseId={int(external_id)}")
        if not _is_success(payload):
            raise RuntimeError(payload.get("message") or f"unexpected response: {payload}")
        data = payload.get("data") or {}
        return data

    def _ensure_target_ready(self, external_id: str, data: dict) -> dict:
        """Provision the per-team target when the challenge needs one.

        ``isNeedInit=true`` challenges have empty ``endpoints`` until
        ``build-exercise-env`` runs and the detail is polled until the endpoints
        are populated. Relying on ``isNeedCheck`` alone is not enough: it can
        flip to ``false`` before the endpoint info appears, which would leave the
        origin without a target and strand the agent.
        """
        needs_target = bool(data.get("isNeedInit"))
        if needs_target and not data.get("endpoints"):
            self._post("/ctf/build-exercise-env", {"exerciseId": int(external_id)})
        return self._poll_until_ready(external_id, data, needs_target=needs_target)

    def _poll_until_ready(self, external_id: str, data: dict, needs_target: bool = False) -> dict:
        deadline = time.monotonic() + self.env_timeout
        while time.monotonic() < deadline:
            ready = not data.get("isNeedCheck")
            if needs_target:
                ready = ready and bool(data.get("endpoints"))
            if ready:
                return data
            time.sleep(self.env_poll_interval)
            data = self._fetch_detail(external_id)
        raise RuntimeError(f"challenge {external_id} target not ready within {self.env_timeout}s")

    @staticmethod
    def _compose_target(endpoints: list | None) -> str:
        if not endpoints:
            return ""
        lines: list[str] = []
        for ep in endpoints:
            ips = ", ".join(str(i) for i in ep.get("exposeIps") or [])
            ports = ", ".join(str(p) for p in ep.get("ports") or [])
            users = "; ".join(
                f"{u.get('username')}/{u.get('password')}" for u in ep.get("users") or []
            )
            mapping = ", ".join(
                f"{m.get('type')}:{m.get('port')}->{m.get('proxy')}" for m in ep.get("portMappings") or []
            )
            bits = []
            if ips:
                bits.append(f"IP={ips}")
            if ports:
                bits.append(f"ports={ports}")
            if mapping:
                bits.append(f"proxyMapping={mapping}")
            if ep.get("isProxy"):
                bits.append("preferProxy=true")
            if users:
                bits.append(f"credentials={users}")
            if ep.get("expireTime"):
                bits.append(f"expire={ep['expireTime']}")
            if bits:
                lines.append("; ".join(bits))
        return "\n".join(lines)


def _collect_attachments(attachment) -> list[str]:
    """Collect download URLs from both the flat and ``files[]`` shapes."""
    if not attachment:
        return []
    urls: list[str] = []
    if isinstance(attachment, dict):
        if isinstance(attachment.get("files"), list):
            for f in attachment["files"]:
                if f.get("url"):
                    urls.append(str(f["url"]))
        elif attachment.get("url"):
            urls.append(str(attachment["url"]))
    return urls
