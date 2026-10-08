"""Bounded autonomous research worker.

Consumes ``queued`` research sessions by claiming them transactionally, runs a
tool-enabled Claude Code research step inside a real bwrap filesystem sandbox
(authorized repo read-only, session workspace writable, no host home). It reuses the
operator's existing host Claude Code configuration (no secrets injected into the
model args), enforces remaining budget by reserving usage and writing back actual
cost/elapsed via the transactional usage ledger, honors pause by terminating the
process, stops if the lease is lost, and records evidence / findings / events before
releasing the lease.

This worker runs as a separate process (``cairn research-worker``) operating directly
on the same SQLite database as the Web service. Only a real claimed run producing
real events counts as M1 — this module is the execution layer, not the whole product.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import socket
import time
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.prompting import render_prompt
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers import get_driver
from cairn.server import db, research_services as service
from cairn.server.research_sandbox import build_sandbox

LOG = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 120
HEARTBEAT_INTERVAL_SECONDS = 10
TERM_GRACE_SECONDS = 8
MAX_OUTPUT_CHARS = 48_000
MAX_SUMMARY_CHARS = 8_000
MAX_RECOVERABLE_RETRIES = 2
RESEARCH_PROMPT = "research.md"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def worker_scoped_env() -> dict[str, str]:
    """Minimal env for the host ``claude`` so it keeps finding its user config.

    Copies HOME/XDG so the CLI reads the operator's existing settings; never copies
    ANTHROPIC_*/CLAUDE_* or auth tokens into the process env (those are read by the
    CLI from its own config). Actual filesystem confinement is handled by bwrap.
    """
    allow = {
        "ALL_PROXY", "HOME", "HTTPS_PROXY", "HTTP_PROXY", "LANG", "LC_ALL",
        "NO_PROXY", "PATH", "SSL_CERT_DIR", "SSL_CERT_FILE", "TEMP", "TMP",
        "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allow}


def _driver_env() -> dict[str, str]:
    from cairn.dispatcher.config import RESERVED_RUNTIME_ENV_KEYS

    return {
        key: value
        for key, value in os.environ.items()
        if key not in RESERVED_RUNTIME_ENV_KEYS and not key.startswith("CAIRN_SAFETY_")
    }


class ResearchWorker:
    def __init__(
        self,
        *,
        db_path: Path | None = None,
        workspace_root: Path | None = None,
        interval_seconds: int = 5,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        worker_id: str | None = None,
        driver: str | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else db.DEFAULT_DB
        self.workspace_root = (
            Path(workspace_root).expanduser()
            if workspace_root
            else Path.home() / ".local" / "share" / "cairn" / "research-workspaces"
        )
        self.interval_seconds = max(2, int(interval_seconds))
        self.lease_seconds = max(30, int(lease_seconds))
        self.hostname = socket.gethostname()
        self.worker_id = worker_id or f"research-worker:{self.hostname}:{uuid4().hex[:8]}"
        # Pluggable agent driver (reuses the shared dispatcher registry + adapters).
        # claudecode is the default and the only fully sandbox-verified driver;
        # other agents (codex/pi/mock) reuse their existing build_execute/envelope
        # integration but their real research-output quality is not yet validated.
        driver_name = driver or os.environ.get("CAIRN_RESEARCH_DRIVER") or "claudecode"
        self._driver = get_driver(driver_name, execution="local")

    # -- lifecycle ---------------------------------------------------------

    def run(self, *, once: bool = False) -> None:
        db.configure(self.db_path)
        if shutil.which("bwrap") is None:
            LOG.error("research worker refuses to run without bubblewrap isolation")
            raise RuntimeError("研究执行需要 bubblewrap 隔离；请安装 bwrap 后重试")
        LOG.info("research worker started %s workspace_root=%s", self.worker_id, self.workspace_root)
        while True:
            try:
                self.tick()
            except Exception as exc:  # keep the durable worker alive across one bad tick
                LOG.exception("research worker tick failed error=%s", exc)
            if once:
                return
            time.sleep(self.interval_seconds)

    def tick(self) -> None:
        self._heartbeat(state="idle", current_session_id=None)
        session = self._claim()
        if session is None:
            return
        session_id = session["id"]
        self._heartbeat(state="working", current_session_id=session_id)
        LOG.info("claimed research session session=%s worker=%s", session_id, self.worker_id)
        try:
            self.run_session(session_id)
        except Exception as exc:
            LOG.exception("research session run crashed session=%s", session_id)
            self._finish(session_id, "failed", f"{type(exc).__name__}: {exc}")
        finally:
            self._heartbeat(state="idle", current_session_id=None)

    # -- claim / helpers ---------------------------------------------------

    def _claim(self):
        with db.get_conn() as conn:
            return service.claim_session(conn, self.worker_id, lease_seconds=self.lease_seconds)

    def _heartbeat(self, *, state: str, current_session_id: str | None) -> None:
        with db.get_conn() as conn:
            service.worker_heartbeat(
                conn, self.worker_id, self.hostname, state=state, current_session_id=current_session_id
            )

    def _reserve(self, session_id: str, *, steps: int = 0, requests: int = 0, elapsed: int = 0,
                 cost: float = 0.0) -> dict | None:
        """Pre-execution usage gate; a 402 means the remaining budget is exhausted."""
        try:
            with db.get_conn() as conn:
                return service.reserve_usage(
                    conn, session_id, self.worker_id,
                    steps=steps, requests=requests, elapsed_seconds=elapsed, cost_usd=cost,
                )
        except Exception as exc:
            LOG.info("budget gate refused session=%s error=%s", session_id, exc)
            return None

    def _account(self, session_id: str, *, steps: int = 0, requests: int = 0, elapsed: int = 0,
                 cost: float = 0.0) -> None:
        """Post-run truthful write-back of actual cost/elapsed; never raises on overrun."""
        try:
            with db.get_conn() as conn:
                service.account_usage(
                    conn, session_id, self.worker_id,
                    steps=steps, requests=requests, elapsed_seconds=elapsed, cost_usd=cost,
                )
        except Exception as exc:
            LOG.warning("usage write-back failed session=%s error=%s", session_id, exc)

    def _lease_heartbeat(self, session_id: str) -> bool:
        """Refresh the research lease; return False if ownership moved or run went away."""
        try:
            owned = False
            with db.get_conn() as conn:
                owned = service.heartbeat(
                    conn, session_id, self.worker_id, lease_seconds=self.lease_seconds
                )
            self._heartbeat(state="working", current_session_id=session_id)
            return owned
        except Exception as exc:
            LOG.warning("research heartbeat failed session=%s error=%s", session_id, exc)
            return False

    def _session_status(self, session_id: str) -> str | None:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM research_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return row[0] if row is not None else None

    def _finish(self, session_id: str, status: str, error: str | None) -> None:
        with db.get_conn() as conn:
            service.finish_run(conn, session_id, self.worker_id, status, latest_error=error or None)

    def _fail(self, session_id: str, category: str, recoverable: bool, reason: str) -> None:
        """M4 bounded recovery: record a classified failure. Recoverable transient
        failures (provider rate-limit / timeout) auto-re-queue the session for the
        next tick up to a cap; permanent failures leave it failed."""
        with db.get_conn() as conn:
            requeued = service.fail_session(
                conn, session_id, self.worker_id,
                category=category, recoverable=recoverable, reason=reason,
                max_retries=MAX_RECOVERABLE_RETRIES,
            )
        if requeued:
            LOG.info("research session bounded-recovery requeue session=%s category=%s", session_id, category)

    def _auto_finalize_if_exhausted(self, session_id: str) -> None:
        """M1 close-out: if a just-paused step has drained the budget (cost/seconds/
        requests) or hit the max_steps cap, auto-close into completed + report instead
        of parking the session paused awaiting a manual resume."""
        try:
            with db.get_conn() as conn:
                out = service.auto_finalize_if_exhausted(conn, session_id)
        except Exception as exc:  # pragma: no cover - never let close-out crash the tick
            LOG.warning("auto-finalize failed session=%s error=%s", session_id, exc)
            return
        if out and out.get("finalized"):
            LOG.info("research session auto-finalized session=%s report=%s", session_id, out.get("report_id"))

    # -- workspace ----------------------------------------------------------

    def _workspace(self, session_id: str) -> Path:
        workspace = (self.workspace_root / session_id.replace("/", "-")).resolve()
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        return workspace

    # -- session execution ---------------------------------------------------

    def run_session(self, session_id: str) -> None:
        with db.get_conn() as conn:
            session = service.get_session(conn, session_id)
        run_id = session.get("current_run_id")  # the run batch opened by claim_session
        workspace = self._workspace(session_id)
        budget = session.get("budget") or {}
        usage = session.get("usage") or {}
        authorization = session.get("authorization") or {}

        # Remaining budget = approved minus already-accounted.
        remaining_cost = max(0.0, float(budget.get("max_cost_usd", 0.0)) - float(usage.get("cost_usd", 0.0)))
        remaining_seconds = max(0, budget.get("minutes", 45) * 60 - int(usage.get("elapsed_seconds", 0)))
        remaining_requests = max(0, budget.get("requests", 0) - int(usage.get("requests", 0)))

        # Gate: reserve one step before starting. 402 means remaining budget exhausted.
        reservation = self._reserve(session_id, steps=1, requests=1)
        if reservation is None:
            self._finish(session_id, "failed", "预算已耗尽；需要明确追加预算后才能继续")
            return
        usage = reservation  # reflect gates so far for reporting

        # Gate: never start a step with no request budget left.
        if remaining_requests <= 0:
            self._finish(session_id, "failed", "请求预算已耗尽，无法开始新的研究步骤")
            return

        prompt = self._build_prompt(session, budget, usage, authorization, remaining_seconds)
        argv = self._driver_argv(prompt, remaining_cost=remaining_cost, session_id=session_id)
        claude_bin = os.environ.get("CAIRN_CLAUDE_BIN") or self._driver.local_binary() or "claude"
        boundary = self._build_sandboxed_argv(workspace, session, argv, claude_bin=claude_bin)

        if remaining_seconds <= 0 or remaining_cost <= 0:
            self._finish(session_id, "failed", "剩余预算不足，无法开始新的研究步骤")
            return

        LOG.info(
            "starting research claude session=%s workspace=%s remaining_cost=%s remaining_seconds=%s",
            session_id, workspace, remaining_cost, remaining_seconds,
        )
        # Outbound enforcement is mandatory for a real target run: if it cannot be
        # established the run FAILS (never silently runs with uncontrolled egress).
        self._active_egress = False
        egress_proxy, egress_env = None, {}
        try:
            egress_proxy, egress_env = self._egress_environment(
                session, remaining_requests, config_root=workspace.parent / ".cairn-sandbox-private"
            )
        except Exception as exc:
            self._finish(session_id, "failed", f"出站控制无法建立，拒绝放行研究执行：{exc}")
            return
        self._active_egress = egress_proxy is not None
        # LD_PRELOAD must be injected INSIDE the sandbox via bwrap --setenv (after the
        # /claude-config mount exists). Putting it in the outer env breaks bwrap's
        # user-namespace helper, which tries to load the interceptor before the sandbox
        # mount is active. The network namespace is the egress boundary; the preload
        # is defense in depth for dynamically linked direct connects.
        preload_path = getattr(self, "_egress_preload_path", None)
        if preload_path and "--" in boundary:
            i = boundary.index("--")
            # The library lives in the read-only /cairn-egress mount (staged with
            # the bridge). Do not bind it under /claude-config: that directory is
            # absent for CAIRN_CLAUDE_BIN runs and writable for real Claude runs.
            boundary[i:i] = ["--setenv", "LD_PRELOAD", preload_path]
        bridge_dir = getattr(self, "_egress_bridge_dir", None)
        if egress_proxy is not None:
            if bridge_dir is None:
                self._finish(session_id, "failed", "出站控制无法建立，拒绝放行研究执行：缺少网络隔离桥")
                egress_proxy.stop()
                return
            from cairn.server.research_egress import inject_egress_bridge
            boundary = inject_egress_bridge(boundary, bridge_dir)
        run_env = worker_scoped_env()
        run_env.update(egress_env)
        process = LocalProcess(
            boundary, cwd=str(workspace), env=run_env,
            timeout_seconds=remaining_seconds or 2700,
            term_grace_seconds=TERM_GRACE_SECONDS,
            stdin=getattr(self, "_prompt_stdin", None),
        )
        started = time.monotonic()
        process.start()
        try:
            process_result = self._communicate_checking_pause(session_id, process)
        finally:
            if egress_proxy is not None:
                try:
                    egress_proxy.stop()
                except Exception as exc:  # pragma: no cover
                    LOG.warning("egress proxy stop failed error=%s", exc)
        elapsed = int(time.monotonic() - started)

        LOG.info(
            "research claude finished session=%s exit=%s cancelled=%s timed_out=%s",
            session_id, process_result.returncode, process_result.cancelled, process_result.timed_out,
        )
        reported_cost = self._provider_cost_from_output(process_result)
        # The authoritative target-request count is the egress proxy's admitted count
        # when enforcement is active; otherwise (tests/fake) it is the http evidence
        # recorded by the payload.
        egress_requests = egress_proxy.quota.requests if egress_proxy is not None else 0
        # Reconciled per-batch usage + cost on EVERY path (pause, timeout, crash,
        # parse failure, and success): elapsed always, any provider cost, and if a
        # cleanly-exited step produced no final cost it is held as 费用待核对.
        clean_exit = (not process_result.cancelled and not process_result.timed_out
                      and process_result.returncode == 0)
        self._settle(session_id, run_id, reported_cost=reported_cost, elapsed=elapsed,
                     requests=egress_requests,
                     expect_final_cost=(clean_exit and reported_cost is None))

        if process_result.cancelled:
            if process_result.cancel_reason in ("pause_requested",):
                self._finish(session_id, "paused", None)
                LOG.info("research session paused session=%s", session_id)
            else:
                # lease lost or run no longer owned/running: the task is gone; do
                # NOT mark it completed or claim evidence produced after the loss.
                self._finish(session_id, "failed", f"{process_result.cancel_reason or 'aborted'}")
                LOG.warning("research session aborted session=%s reason=%s", session_id, process_result.cancel_reason)
            return
        if process_result.timed_out or process_result.returncode != 0:
            detail = process_result.stderr.strip() or process_result.stdout.strip() or ""
            fail = service.classify_failure(
                returncode=process_result.returncode,
                timed_out=process_result.timed_out,
                detail=detail,
            )
            self._fail(session_id, fail["category"], fail["recoverable"],
                       f"研究进程异常退出 {process_result.returncode}: {detail[:2000]}")
            return

        payload, _ = self._extract_payload(process_result)
        if payload is None:
            # Malformed / non-object / unparseable output is NOT a completion; mark
            # the run failed rather than crafting a terminal=True report.
            self._fail(session_id, "protocol_error", False,
                       "研究进程返回的输出无法解析为有效的研究结果")
            return

        still_owned, _ = self._apply_payload(session_id, run_id, payload)
        if not still_owned:
            # ownership moved or the payload was rejected (protocol/pause); do not
            # claim completion — settle already filed this batch's real consumption.
            LOG.warning("research results not committed session=%s", session_id)
        else:
            self._auto_finalize_if_exhausted(session_id)

    # -- per-batch settlement -------------------------------------------------

    def _egress_environment(self, session, remaining_requests: int, config_root=None):
        """Build (proxy, env) for a real target run.

        The sandbox network namespace has no external route. This stages a Unix-socket
        bridge to the scoped proxy (the only way out) and compiles the LD_PRELOAD
        guard. A URL session always gets the bridge, including when ``CAIRN_CLAUDE_BIN``
        points at a stand-in binary. Model-gateway traffic is classified separately
        and is not quota-counted. FAILS LOUDLY when the bridge or the preload cannot
        be built.
        """
        url = session.get("url")
        if not url:
            return None, {}
        try:
            from cairn.server.research_egress import (
                DEFAULT_MODEL_HOSTS, SANDBOX_PROXY_PORT, SANDBOX_PROXY_URL,
                EgressProxy, build_egress_preload, extract_gateway_hosts,
                stage_egress_bridge,
            )
        except Exception as exc:
            raise RuntimeError(f"出站强制模块不可用：{exc}")
        # Model gateway traffic is classified separately (allowed, not quota-counted)
        # so the research model can always reach its own provider.
        model_hosts = set(DEFAULT_MODEL_HOSTS)
        model_hosts.update(extract_gateway_hosts(os.environ.get("HOME")))
        auth = session.get("authorization") or {}
        proxy = EgressProxy(allow=[url], block=auth.get("excluded") or [],
                            request_quota=max(0, int(remaining_requests)),
                            model_hosts=sorted(model_hosts))
        try:
            proxy.start()
        except OSError as exc:
            raise RuntimeError(f"无法启动本地出站代理：{exc}")
        # Compile beside the bridge, then copy the library into the read-only
        # egress mount. A host /tmp path would be hidden by bwrap's fresh /tmp,
        # and /claude-config is not mounted when CAIRN_CLAUDE_BIN is set.
        if config_root is not None:
            compile_dir = Path(config_root) / "egress-preload"
            compile_dir.mkdir(parents=True, exist_ok=True)
            so = build_egress_preload(compile_dir)
        else:
            so = build_egress_preload()
        if so is None:
            proxy.stop()
            # The network namespace is the boundary, and the preload refuses UDP and
            # non-proxy loopback for dynamically linked processes. Do not run without it.
            raise RuntimeError("缺少 C 编译器，无法建立出站强制（直接连接可能绕过代理）；拒绝放行研究执行")
        if config_root is None:
            proxy.stop()
            raise RuntimeError("出站桥需要私有配置目录；拒绝放行研究执行")
        try:
            self._egress_bridge_dir = stage_egress_bridge(config_root, proxy, preload_so=so)
        except OSError as exc:
            proxy.stop()
            raise RuntimeError(f"无法建立出站网络隔离桥：{exc}") from exc
        # The interceptor is referenced by its SANDBOX-internal path and injected via
        # bwrap --setenv by the caller (NOT in the outer env — see run_session).
        # Proxy env points at the in-sandbox forwarder, not the host TCP listener.
        self._egress_preload_path = "/cairn-egress/libcairn_egress.so"
        self._egress_preload_host = None
        env = {
            "HTTP_PROXY": SANDBOX_PROXY_URL,
            "HTTPS_PROXY": SANDBOX_PROXY_URL,
            "ALL_PROXY": SANDBOX_PROXY_URL,
            "http_proxy": SANDBOX_PROXY_URL,
            "https_proxy": SANDBOX_PROXY_URL,
            "all_proxy": SANDBOX_PROXY_URL,
            "CAIRN_EGRESS_PROXY": f"127.0.0.1:{SANDBOX_PROXY_PORT}",
        }
        return proxy, env

    def _settle(self, session_id: str, run_id: str | None, *, reported_cost: float | None,
                elapsed: int, requests: int = 0, expect_final_cost: bool = False) -> None:
        """Idempotently finalize this run batch against the session budget. ``run_id``
        is the batch opened at claim; cost is only applied truthfully, and when a
        completed step left no final cost the session is held as 费用待核对."""
        try:
            with db.get_conn() as conn:
                service.settle_run(
                    conn, session_id, run_id, self.worker_id,
                    elapsed_seconds=max(0, elapsed), requests=max(0, requests),
                    final_cost=reported_cost, expect_final_cost=expect_final_cost,
                )
        except Exception as exc:
            LOG.warning("run settlement failed session=%s run=%s error=%s", session_id, run_id, exc)
    # -- parsing ---------------------------------------------------------------

    def _provider_cost_from_output(self, process_result: ProcessResult) -> float | None:
        """Best-effort total_cost_usd from the raw outer claude envelope, even when
        the run did not finish cleanly. Returns None if it could not be parsed."""
        try:
            payload = json.loads(process_result.stdout)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        cost = payload.get("total_cost_usd")
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(cost) or cost < 0:
            return None
        return cost

    def _extract_payload(self, process_result: ProcessResult) -> tuple[dict | None, dict | None]:
        """Parse the outer Claude envelope (result / structured_output) into the
        research payload, reusing the driver that already knows the real envelope.

        Only a structurally valid dict counts. Malformed, unparseable, or non-object
        output returns ``(None, None)`` so the caller marks the run failed instead of
        fabricating a ``terminal=True`` completion report."""
        try:
            analysis = self._driver.extract_analysis_response(
                process_result.stdout, process_result.stderr
            )
        except Exception as exc:
            LOG.warning("research envelope parse failed error=%s", exc)
            return None, None
        try:
            payload = json.loads(analysis.text)
        except Exception as exc:
            LOG.warning("research structured output is not JSON text error=%s", exc)
            return None, None
        if not isinstance(payload, dict):
            LOG.warning("research structured output is not an object: %s", type(payload).__name__)
            return None, None
        # A valid run must state terminal explicitly; missing means "not yet done" so
        # a partial/uncertain answer can never auto-complete.
        if payload.get("terminal") is None:
            payload["terminal"] = False
        return payload, analysis.metadata

    # -- claude invocation -----------------------------------------------------

    def _build_sandboxed_argv(self, workspace: Path, session, argv: list[str], *, claude_bin: str = "claude") -> list[str]:
        repo = session.get("repo") if session.get("repo") else None
        config_src = None
        config_root = workspace.parent / ".cairn-sandbox-private"
        if not os.environ.get("CAIRN_CLAUDE_BIN") and self._driver.type_name == "claudecode":
            # Only seed the operator's real Claude config for the real claude binary,
            # not for a test override or another agent driver. The config is staged
            # OUTSIDE the workspace and mounted read-only.
            config_src = Path(os.environ.get("HOME", "/root")) if os.environ.get("HOME") else None
        return build_sandbox(
            workspace=workspace, repo=repo, argv=argv, claude_bin=claude_bin,
            claude_config_src=config_src, config_root=config_root,
        )

    def _build_claude_argv(self, prompt: str, *, remaining_cost: float) -> list[str]:
        # Run non-interactively, emit JSON, and auto-approve tools INSIDE the sandbox:
        # the real boundary is the bwrap filesystem plus a network namespace whose only
        # exit is the scoped egress proxy. Bypassing Claude's own permission prompts
        # lets the model run Bash/curl against the authorized target. Outside the
        # sandbox this flag would be dangerous; here file access and egress are bounded.
        argv = ["--output-format", "json", "-p", "--permission-mode", "bypassPermissions"]
        if remaining_cost > 0:
            argv += ["--max-budget-usd", f"{remaining_cost:.6f}"]
        argv += ["--", prompt]
        return argv

    def _driver_argv(self, prompt: str, *, remaining_cost: float, session_id: str) -> list[str]:
        """Build the agent invocation argv for the selected driver. claudecode keeps
        its research-tuned args (the sandbox-verified default); other drivers reuse
        their existing ``build_execute`` native non-interactive invocation. Either way
        the bwrap filesystem + scoped/counted egress proxy remain the real boundary."""
        if self._driver.type_name == "claudecode":
            self._prompt_stdin = None
            return self._build_claude_argv(prompt, remaining_cost=remaining_cost)
        cfg = WorkerConfig(
            name="research", type=self._driver.type_name,
            task_types=["vulnerability_analysis"], max_running=1, priority=0,
            env=_driver_env(),
        )
        result = self._driver.build_execute(cfg, prompt, session_id)
        self._prompt_stdin = result.stdin
        return list(result.argv)

    def _load_prompt(self) -> str:
        return resources.files("cairn.dispatcher.prompts").joinpath("default").joinpath(
            RESEARCH_PROMPT
        ).read_text(encoding="utf-8")

    def _build_prompt(self, session, budget, usage, authorization, remaining_seconds) -> str:
        template = self._load_prompt()
        evidence_summary = []
        for item in session.get("evidence", []):
            evidence_summary.append(f"- [{item['kind']}] {item['title']}: {item['content'][:500]}")
        for finding in session.get("findings", []):
            evidence_summary.append(f"- finding({finding['status']}) {finding['title']}")
        budget_for_prompt = {**budget, "remaining_seconds": remaining_seconds}
        experience_summary = []
        try:
            with db.get_conn() as conn:
                for exp in service.list_experiences_for_scope(
                    conn, service.session_scope(session), session_id=session.get("id"), limit=20
                ):
                    experience_summary.append(
                        f"- [{exp['kind']}] {exp['content']}（来源会话 {exp['source_session_id']} · {exp.get('ref') or '无引用'}）"
                    )
        except Exception as exc:  # pragma: no cover - seeding must never block a run
            LOG.warning("experience seeding failed session=%s error=%s", session.get("id"), exc)
        return render_prompt(
            template,
            {
                "authorization_json": _json(authorization),
                "repo": session.get("repo") or "无",
                "url": session.get("url") or "无",
                "objective": session.get("objective", ""),
                "next_direction": session.get("next") or session.get("next_direction") or "",
                "budget_json": _json(budget_for_prompt),
                "usage_json": _json(usage),
                "evidence_summary": "\n".join(evidence_summary) or "（暂无）",
                "experience_summary": "\n".join(experience_summary) or "（暂无跨会话经验）",
            },
        )

    def _communicate_checking_pause(self, session_id: str, process: LocalProcess) -> ProcessResult:
        """Wait for claude while heartbeating the lease and watching for both a
        user-requested pause AND lease/ownership loss; terminate the process group
        in either case so a task it no longer owns never keeps running."""
        from concurrent.futures import Future, ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=1)
        future: Future = executor.submit(process.communicate, None)
        try:
            while not future.done():
                status = self._session_status(session_id)
                owned = self._lease_heartbeat(session_id)
                if status == "pause_requested":
                    LOG.info("pause requested session=%s terminating process", session_id)
                    process.cancel("pause_requested")
                    break
                if not owned:
                    LOG.warning("lease lost session=%s stopping process", session_id)
                    process.cancel("lease_lost")
                    break
                if status not in ("running",):
                    LOG.warning("session no longer running session=%s status=%s stopping", session_id, status)
                    process.cancel(f"status={status}")
                    break
                time.sleep(min(HEARTBEAT_INTERVAL_SECONDS, self.interval_seconds or 5))
        finally:
            result = future.result()
            executor.shutdown(wait=False)
        return result

    # -- structured output ---------------------------------------------------

    def _apply_payload(self, session_id: str, run_id: str | None, payload: dict) -> tuple[bool, int]:
        """Single-transaction commit of a run batch's results (item ③).

        Delegates to ``commit_run_results`` which, under one SQLite writer transaction,
        validates run_id / ownership / lease / session state and then writes evidence,
        findings, events, checkpoint, terminal state and the auto report all-or-nothing
        (strict payload validation happens in the same commit before any write). Returns
        ``(still_owned, target_requests)``: a stale worker that lost ownership writes
        ZERO results and the batch's real consumption is still filed at settlement.
        """
        target_requests = 0
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            target_requests = sum(
                1 for item in evidence if str(item.get("kind")) in ("http", "request")
            )
        # Attribute real target-request consumption BEFORE the terminal commit clears
        # the lease (account_usage requires lease_owner==worker). When egress
        # enforcement is active the authoritative count is the proxy's admitted target
        # requests (already applied by settle before this); here we only count http
        # evidence when no proxy is in play (/test-fake runs) so requests are never
        # double-counted.
        if target_requests and not getattr(self, "_active_egress", False):
            self._account(session_id, requests=target_requests)
        result = None
        try:
            with db.get_conn() as conn:  # one transaction: validate + write + report
                result = service.commit_run_results(conn, session_id, run_id, self.worker_id, payload)
        except Exception as exc:
            LOG.warning("commit_run_results failed session=%s error=%s", session_id, exc)
            return False, target_requests
        if not result.get("ok"):
            reason = result.get("reason")
            LOG.warning("research results not committed session=%s reason=%s",
                        session_id, reason or result.get("detail"))
            if reason == "protocol_error":
                self._finish(session_id, "failed", result.get("detail") or "研究结果不符合协议")
            elif reason == "paused":
                self._finish(session_id, "paused", None)
            # ownership_lost: a stale worker files nothing; settlement already filed
            # this batch's real consumption.
            return False, target_requests
        return True, target_requests