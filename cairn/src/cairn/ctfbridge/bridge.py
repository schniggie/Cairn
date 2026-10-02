"""CTF bridge main loop.

The bridge is a standalone process that drives Cairn exclusively through the
Cairn HTTP API:

1. read ``/ctf/config`` — idle unless ``mode == ctf`` and ``base_url`` set;
2. sync: pull the platform challenge list, diff against ``ctf_challenges`` by
   ``external_id``, insert new rows as ``queued``, refresh metadata of known
   ones. Full per-challenge detail (which may provision a target env) is fetched
   lazily right before project creation — never eagerly at sync time;
3. create Cairn projects for queued challenges up to ``max_concurrent``;
4. watch created projects; on completion extract a flag matching
   ``flag_regex`` from the facts and submit it to the platform;
5. handle wrong flags by reopening the project (bounded by
   ``submission_max_retries``) and rate limits with exponential backoff.

All challenge state lives in the server database, so a restart resumes the
queue without re-creating projects.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests

from cairn.ctfbridge.adapters import get_adapter
from cairn.ctfbridge.adapters.base import ChallengeSource, SubmissionResult
from cairn.ctfbridge.client import BridgeError, CairnApi
from cairn.ctfbridge.config import BridgeConfig, load_bridge_config

log = logging.getLogger("cairn.ctfbridge.bridge")

MAX_ATTEMPTS = 5  # default wrong-flag reopen cycles (overridable via config)
RATE_LIMIT_BACKOFF_SECONDS = 30  # default base for exponential backoff
MODEL_PROBE_INTERVAL_SECONDS = 60  # how often to ping the LLM for health

# Statuses that hold a live, actively-worked project. Rate-limited / submitted
# challenges have completed projects and are just waiting for a retry, so they
# must NOT block new projects.
_ACTIVE_STATUSES = ("solving",)

# Cooldown after a project-creation failure before retrying the same challenge
# (e.g. a challenge that is listed but not yet released on the platform).
_PROJECT_CREATE_COOLDOWN = 120.0

# Flag strings that are almost certainly placeholders / hallucinations and
# should never be submitted (they burn per-challenge attempts).
_SUSPECT_FLAG_HINTS = (
    "ni_cai",
    "你猜",
    "占位",
    "placeholder",
    "example",
    "test",
    "xxx",
    "fake",
    "待定",
    "todo",
    "null",
)
_MIN_PLAUSIBLE_FLAG_LEN = 8


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag_is_plausible(flag: str) -> bool:
    """Reject placeholder / hallucinated flags before they are submitted."""
    low = flag.lower()
    if any(hint in low for hint in _SUSPECT_FLAG_HINTS):
        return False
    if len(flag) < _MIN_PLAUSIBLE_FLAG_LEN:
        return False
    return True


class CtfBridge:
    def __init__(
        self,
        server_url: str,
        *,
        api: CairnApi | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        now_fn=time.monotonic,
    ) -> None:
        self.api = api or CairnApi(server_url)
        self.max_attempts = max_attempts
        self._now = now_fn
        self._rate_limit_retry_at: dict[str, float] = {}
        self._project_create_retry_at: dict[str, float] = {}
        self._env_recovered: set[str] = set()
        self._last_model_probe: float = 0.0
        self._model_ok: bool | None = None
        self._model_error: str | None = None
        self.cfg: BridgeConfig | None = None
        self.source: ChallengeSource | None = None
        self._source_key: tuple | None = None
        self.stats: dict = {}

    # -------------------------------------------------------------- lifecycle

    def run(self) -> None:
        """Run forever, sleeping ``poll_interval`` between rounds."""
        while True:
            interval = self.cfg.poll_interval if self.cfg else 10
            self.run_once()
            time.sleep(max(1, interval))

    def run_once(self) -> dict:
        """Execute a single round and return the round stats (for tests)."""
        self.stats = {"created": [], "submitted": [], "synced": False, "error": None}
        try:
            self.cfg = load_bridge_config(self.api)
            self._report_heartbeat(error=None)
        except BridgeError as exc:
            self.stats["error"] = str(exc)
            log.warning("could not read config: %s", exc)
            self._report_heartbeat(error=str(exc))
            return self.stats

        cfg = self.cfg
        if not cfg.is_active():
            log.info("idle: mode=%s base_url=%r", cfg.mode, cfg.base_url)
            return self.stats

        # Keep the same adapter instance across rounds so stateful platforms
        # (rate-limit budgets, auth sessions) survive; rebuild on config change.
        key = (cfg.adapter, cfg.base_url, cfg.token, cfg.team_name)
        if self._source_key != key:
            self.source = get_adapter(
                cfg.adapter,
                base_url=cfg.base_url,
                token=cfg.token,
                team_name=cfg.team_name,
                env_poll_interval=cfg.env_poll_interval,
                env_timeout=cfg.env_timeout,
            )
            self._source_key = key

        should_sync = cfg.sync_requested or self._sync_overdue(cfg)
        if should_sync:
            try:
                self._sync(cfg)
                self.stats["synced"] = True
            except Exception as exc:  # noqa: BLE001 - report and keep running
                self.stats["error"] = f"sync failed: {exc}"
                log.error("sync failed: %s", exc)
                self._report_heartbeat(error=str(exc))

        try:
            self._check_model_health(cfg)
            self._manage_projects(cfg)
            self._check_completed(cfg)
            self._retry_rate_limited(cfg)
            self._recover_idle_envs(cfg)
        except Exception as exc:  # noqa: BLE001
            self.stats["error"] = str(exc)
            log.error("round failed: %s", exc)
            self._report_heartbeat(error=str(exc))
            return self.stats

        self._report_heartbeat(error=self.stats.get("error"))
        return self.stats

    # ------------------------------------------------------------------ sync

    def _sync_overdue(self, cfg: BridgeConfig) -> bool:
        if not cfg.last_sync_at:
            return True
        try:
            last = datetime.strptime(cfg.last_sync_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= cfg.poll_interval

    def _sync(self, cfg: BridgeConfig) -> None:
        """Pull the platform list and reconcile local rows.

        Deliberately does NOT call ``get_challenge`` here: for dynamic-target
        platforms that call provisions a target env, and doing so for every
        challenge during a sync would spin up dozens of targets at once. Full
        detail is fetched lazily in :meth:`_refresh_queued_row` right before a
        project is created.
        """
        remote = {c.external_id: c for c in self.source.list_challenges()}
        known = {row["external_id"]: row for row in self.api.list_challenges()}
        now = _iso_now()

        for external_id, remote_challenge in remote.items():
            existing = known.get(external_id)
            if existing is None:
                self.api.create_challenge(
                    external_id=external_id,
                    title=remote_challenge.title,
                    category=remote_challenge.category,
                    points=remote_challenge.points,
                    needs_refresh=1,
                    created_at=now,
                    updated_at=now,
                )
                log.info("new challenge (detail deferred): [%s] %s", remote_challenge.category, remote_challenge.title)
            else:
                self.api.update_challenge(
                    existing["id"],
                    title=remote_challenge.title,
                    category=remote_challenge.category,
                    points=remote_challenge.points,
                    updated_at=now,
                )

        self.api.ack_sync(synced_at=now)

    # ------------------------------------------------------------------ solve

    def _manage_projects(self, cfg: BridgeConfig) -> None:
        rows = self.api.list_challenges()
        active_count = sum(1 for r in rows if r["status"] in _ACTIVE_STATUSES)
        queued = sorted(
            (r for r in rows if r["status"] == "queued"),
            key=lambda r: r["created_at"],
        )
        now = self._now()
        for row in queued:
            if active_count >= cfg.max_concurrent:
                break
            # Cooldown: a challenge that failed to provision (e.g. listed but
            # not yet released) is skipped this cycle instead of blocking the
            # rest of the queue.
            if self._project_create_retry_at.get(row["external_id"], 0) > now:
                continue
            # Idempotency: never build a second project for a challenge that
            # already has a live project (prevents orphan projects on requeue).
            if row.get("project_id"):
                log.warning("challenge %s already has project %s; skipping", row["external_id"], row["project_id"])
                continue
            try:
                self._refresh_queued_row(cfg, row)
                project_id = self._create_solve_project(cfg, row)
            except Exception as exc:  # noqa: BLE001
                # Do NOT break the whole queue: a single challenge failing to
                # provision must not block later (open) challenges.
                log.error("failed to create project for %s: %s", row["external_id"], exc)
                self.stats["error"] = f"project creation failed: {exc}"
                self._project_create_retry_at[row["external_id"]] = now + _PROJECT_CREATE_COOLDOWN
                continue
            self.api.update_challenge(row["id"], status="solving", project_id=project_id, updated_at=_iso_now())
            self.stats["created"].append(row["external_id"])
            # A fresh project will build a fresh env; forget any prior recovery
            # so the new env is released again on completion/stop.
            self._env_recovered.discard(row["external_id"])
            active_count += 1

    def _recover_env_once(self, external_id: str, reason: str) -> None:
        """Release a challenge's target instance, at most once per env."""
        if external_id in self._env_recovered:
            return
        try:
            self.source.recover_env(external_id)
            self._env_recovered.add(external_id)
            log.info("released env for %s (%s)", external_id, reason)
        except Exception as exc:  # noqa: BLE001
            log.warning("recover env failed for %s: %s", external_id, exc)

    def _recover_idle_envs(self, cfg: BridgeConfig) -> None:
        """Release instances for terminal/paused challenges so solved or stopped
        challenges don't hold platform quota (DasCTF caps teams at 3)."""
        rows = self.api.list_challenges()
        for row in rows:
            if row["status"] in ("solved", "failed", "paused"):
                self._recover_env_once(row["external_id"], row["status"])

    def _refresh_queued_row(self, cfg: BridgeConfig, row: dict) -> dict:
        """Fetch full challenge detail (may provision the target env).

        Called right before project creation so the dashboard shows correct
        points/description/target and envs are only built for challenges that
        are actually started.
        """
        if not row.get("needs_refresh") and row.get("points"):
            return row
        try:
            detail = self.source.get_challenge(row["external_id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("could not refresh detail for %s: %s", row["external_id"], exc)
            return row
        return self.api.update_challenge(
            row["id"],
            title=detail.title,
            category=detail.category,
            points=detail.points,
            description=detail.description,
            target=detail.target,
            attachments=detail.attachments,
            hints=detail.hints,
            needs_refresh=0,
            updated_at=_iso_now(),
        )

    def _create_solve_project(self, cfg: BridgeConfig, row: dict) -> str:
        # Refresh the per-team target right before solving — dynamic instances
        # can change, and a stale host/port would sink the whole challenge.
        detail = self.source.get_challenge(row["external_id"])

        title = f"[{detail.category or 'CTF'}] {detail.points}pt {detail.title}"
        origin = self._build_origin(detail)
        goal = f"找到该题的 flag（形如 {cfg.flag_regex}）并将其提交到 CTF 平台。"

        hints = [{"content": h, "creator": "ctf"} for h in detail.hints]
        project = self.api.create_project(title=title, origin=origin, goal=goal, hints=hints)
        return project["project"]["id"]

    def _build_origin(self, detail) -> str:
        parts = [f"题目描述：\n{detail.description}".strip()]
        if detail.target:
            parts.append(f"目标实例：{detail.target}")
        if detail.attachments:
            parts.append("附件下载：")
            parts.extend(f"- {url}" for url in detail.attachments)
        return "\n".join(parts)

    # ---------------------------------------------------------- completion

    def _check_completed(self, cfg: BridgeConfig) -> None:
        rows = [r for r in self.api.list_challenges() if r["status"] == "solving" and r.get("project_id")]
        for row in rows:
            try:
                project = self.api.get_project(row["project_id"])
            except BridgeError:
                # The Cairn project was deleted out from under us — requeue so a
                # fresh project gets created instead of failing every round.
                log.warning("project %s gone; requeueing %s", row["project_id"], row["external_id"])
                self.api.update_challenge(row["id"], status="queued", project_id=None, updated_at=_iso_now())
                continue

            # Budget guard: stop a project that burns too many agent tasks
            # (concluded intents) so a runaway solve cannot drain the model
            # quota. Tiered by challenge points; budgets are configurable.
            concluded = sum(1 for i in project.get("intents", []) if i.get("to"))
            budget = self._budget_for(cfg, row.get("points") or 0)
            if concluded > budget:
                log.warning(
                    "challenge %s over budget (%d intents > %d); stopping",
                    row["external_id"], concluded, budget,
                )
                self._stop_challenge_over_budget(row)
                self.stats["error"] = f"budget exceeded for {row['external_id']}: {concluded} intents > {budget}"
                continue

            pstatus = project["project"]["status"]
            if pstatus == "active":
                continue
            if pstatus == "stopped":
                # The project was stopped out-of-band. If the challenge is no
                # longer solving (e.g. user paused it), leave it alone; only
                # requeue when it is still expected to run.
                log.warning("project %s stopped; requeueing %s", row["project_id"], row["external_id"])
                self.api.update_challenge(row["id"], status="queued", project_id=None, updated_at=_iso_now())
                continue
            if pstatus != "completed":
                continue

            flag = self._extract_flag(project, cfg.flag_regex)
            if flag is None:
                log.error("project %s completed without a matching flag", row["project_id"])
                self.api.update_challenge(row["id"], status="failed", updated_at=_iso_now())
                continue

            if cfg.auto_submit:
                self._submit_and_handle(cfg, row, flag)
            else:
                log.info("auto_submit disabled; storing flag for %s", row["external_id"])
                self.api.update_challenge(
                    row["id"], status="submitted", last_flag=flag, updated_at=_iso_now()
                )
            self.stats["submitted"].append(row["external_id"])

    def _budget_for(self, cfg: BridgeConfig, points: int) -> int:
        """Per-challenge agent-task budget, tiered by challenge points."""
        if points <= 100:
            return cfg.budget_easy
        if points <= 300:
            return cfg.budget_medium
        return cfg.budget_hard

    def _stop_challenge_over_budget(self, row: dict) -> None:
        """Stop the project and mark the challenge failed after budget breach."""
        try:
            if row.get("project_id"):
                self.api.update_project_status(row["project_id"], "stopped")
        except BridgeError as exc:
            log.warning("failed to stop project for %s: %s", row["external_id"], exc)
        self.api.update_challenge(row["id"], status="failed", updated_at=_iso_now())

    def _extract_flag(self, project: dict, flag_regex: str) -> str | None:
        pattern = re.compile(flag_regex)

        def _candidate(flag: str) -> str | None:
            if flag and _flag_is_plausible(flag):
                return flag
            return None

        completion = next((i for i in project.get("intents", []) if i.get("to") == "goal"), None)

        # 1. The completion conclusion is the worker's final answer.
        if completion:
            match = pattern.search(completion.get("description", ""))
            if match:
                candidate = _candidate(match.group(0))
                if candidate:
                    return candidate

        # 2. Facts that fed the completion, skipping origin/goal boilerplate.
        source_ids = set(completion.get("from", [])) if completion else set()
        for fact in project.get("facts", []):
            if fact["id"] in ("origin", "goal"):
                continue
            if fact["id"] not in source_ids:
                continue
            match = pattern.search(fact.get("description", ""))
            if match:
                candidate = _candidate(match.group(0))
                if candidate:
                    return candidate

        # 3. Any remaining non-boilerplate fact.
        for fact in project.get("facts", []):
            if fact["id"] in ("origin", "goal"):
                continue
            match = pattern.search(fact.get("description", ""))
            if match:
                candidate = _candidate(match.group(0))
                if candidate:
                    return candidate

        # origin/goal are deliberately excluded: they are boilerplate and often
        # contain a literal "flag{...}" format placeholder, which would submit a
        # false positive. A real flag is found in a derived fact or conclusion.
        return None

    def _submit_and_handle(self, cfg: BridgeConfig, row: dict, flag: str) -> None:
        log.info("submitting flag for %s", row["external_id"])
        try:
            result, message = self.source.submit_flag(row["external_id"], flag)
        except Exception as exc:  # noqa: BLE001
            log.error("submit failed for %s: %s", row["external_id"], exc)
            self.api.update_challenge(row["id"], status="failed", updated_at=_iso_now())
            return

        challenge_id = row["id"]
        if result is SubmissionResult.SUCCESS:
            log.info("%s solved", row["external_id"])
            self.api.update_challenge(challenge_id, status="solved", last_flag=flag, updated_at=_iso_now())
            # Free the target instance as soon as the flag is accepted so a
            # solved challenge never holds a slot (DasCTF caps at 3 instances).
            self._recover_env_once(row["external_id"], "solved")
        elif result is SubmissionResult.RATE_LIMITED:
            log.warning("%s rate limited (%s)", row["external_id"], message)
            self._rate_limit_retry_at[row["external_id"]] = self._now()
            self.api.update_challenge(
                challenge_id,
                status="rate_limited",
                last_flag=flag,
                attempt_count=row["attempt_count"],
                updated_at=_iso_now(),
            )
        elif result is SubmissionResult.WRONG_FLAG:
            self._handle_wrong_flag(cfg, row, flag)
        else:
            log.error("%s submit error: %s", row["external_id"], message)
            self.api.update_challenge(challenge_id, status="failed", updated_at=_iso_now())

    def _handle_wrong_flag(self, cfg: BridgeConfig, row: dict, flag: str) -> None:
        max_attempts = cfg.submission_max_retries or self.max_attempts
        attempt_count = row["attempt_count"] + 1
        if attempt_count >= max_attempts:
            log.warning("%s failed after %s attempts", row["external_id"], attempt_count)
            self.api.update_challenge(
                row["id"],
                status="failed",
                last_flag=flag,
                attempt_count=attempt_count,
                updated_at=_iso_now(),
            )
            return
        # The project is completed; reopen it so a worker keeps hunting.
        description = f"提交的 flag {flag} 是错误的，继续寻找正确的 flag。"
        self.api.reopen_project(row["project_id"], description=description)
        self.api.update_challenge(
            row["id"],
            status="solving",
            last_flag=flag,
            attempt_count=attempt_count,
            updated_at=_iso_now(),
        )
        log.info("%s reopened (attempt %d)", row["external_id"], attempt_count)

    # ---------------------------------------------------------- rate limited

    def _retry_rate_limited(self, cfg: BridgeConfig) -> None:
        backoff = cfg.rate_limit_backoff or RATE_LIMIT_BACKOFF_SECONDS
        rows = [r for r in self.api.list_challenges() if r["status"] == "rate_limited" and r.get("last_flag")]
        for row in rows:
            retry_at = self._rate_limit_retry_at.get(row["external_id"])
            if retry_at is not None and self._now() - retry_at < backoff:
                continue
            # Resubmit the stored flag; the project is already completed.
            self._submit_and_handle(cfg, row, row["last_flag"])

    # ------------------------------------------------------------ model health

    def _check_model_health(self, cfg: BridgeConfig) -> None:
        """Ping the configured LLM endpoint and surface failures (e.g. quota).

        The dispatcher is the real caller of the model, but the bridge can
        cheaply probe the configured ``model_base_url`` so the UI can show
        "model unavailable / quota exhausted" instead of silently spinning.
        """
        if not cfg.model_base_url or not cfg.model_name or not cfg.model_api_key:
            self._model_ok = None
            self._model_error = None
            return
        if self._now() - self._last_model_probe < MODEL_PROBE_INTERVAL_SECONDS:
            return
        self._last_model_probe = self._now()

        ok = True
        error: str | None = None
        try:
            resp = requests.post(
                f"{cfg.model_base_url.rstrip('/')}/v1/messages",
                json={
                    "model": cfg.model_name,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={
                    "Authorization": f"Bearer {cfg.model_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if resp.status_code >= 400:
                ok = False
                try:
                    error = resp.json().get("message") or f"HTTP {resp.status_code}"
                except ValueError:
                    error = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            ok = False
            error = str(exc)

        self._model_ok = ok
        self._model_error = error
        if not ok:
            log.warning("model health probe failed: %s", error)

    # --------------------------------------------------------------- helper

    def _report_heartbeat(self, *, error: str | None) -> None:
        try:
            self.api.heartbeat(
                error=error,
                model_ok=self._model_ok,
                model_error=self._model_error,
            )
        except BridgeError:
            log.warning("could not report heartbeat to server")
