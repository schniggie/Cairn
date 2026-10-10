from __future__ import annotations

import json
import logging
import posixpath
from pathlib import PurePosixPath
import shlex
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Collection

from cairn.dispatcher.config import SshEnvironmentConfig, WorkerType
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.registry import get_driver

LOG = logging.getLogger(__name__)
RUNNER_PATH = ".cairn/bin/cairn-runner"
SSH_PREVIEW_LIMIT = 1200
SSH_REMOTE_RUN_ATTEMPTS = 2
SSH_TRANSIENT_MARKERS = (
    "Connection closed by",
    "Connection reset by peer",
    "kex_exchange_identification",
)


RUNNER_SCRIPT = r'''#!/usr/bin/env python3
import argparse
import glob
import json
import os
import select
import signal
import subprocess
import sys
import time


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def execute():
    req = json.load(sys.stdin)
    state_path = req["state_path"]
    cancel_path = state_path + ".cancel"
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    if os.path.exists(cancel_path):
        return 130
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in (req.get("env") or {}).items()})
    process = subprocess.Popen(
        req["argv"],
        cwd=req["cwd"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump({"pid": process.pid, "pgid": pgid, "started_at": _now()}, handle)
    if os.path.exists(cancel_path):
        _kill_pgid(pgid)
        return process.wait()
    timeout = req.get("timeout_seconds")
    kill_after = float(req.get("kill_after_seconds") or 5)
    deadline = time.monotonic() + float(timeout) if timeout else None
    streams = {process.stdout: sys.stdout.buffer, process.stderr: sys.stderr.buffer}
    while streams:
        if deadline is not None and time.monotonic() >= deadline and process.poll() is None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(kill_after)
            if process.poll() is None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return 124
        ready, _, _ = select.select(list(streams), [], [], 0.1)
        for src in ready:
            chunk = src.read1(4096) if hasattr(src, "read1") else src.read(4096)
            if chunk:
                dst = streams[src]
                dst.write(chunk)
                dst.flush()
            else:
                streams.pop(src, None)
        if process.poll() is not None and not ready:
            for src, dst in list(streams.items()):
                chunk = src.read()
                if chunk:
                    dst.write(chunk)
                    dst.flush()
                streams.pop(src, None)
    return process.wait()


def _kill_pgid(pgid):
    if pgid <= 0:
        return 0
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    time.sleep(1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return 0


def cancel(state_path):
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path + ".cancel", "w", encoding="utf-8") as handle:
            json.dump({"cancelled_at": _now()}, handle)
    except OSError:
        pass
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return 0
    pgid = int(state.get("pgid") or 0)
    return _kill_pgid(pgid)


def cancel_workspace(workspace):
    runs_dir = os.path.join(workspace, ".cairn", "runs")
    for state_path in glob.glob(os.path.join(runs_dir, "*", "state.json")):
        cancel(state_path)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("execute")
    cancel_parser = sub.add_parser("cancel")
    cancel_parser.add_argument("state_path")
    cancel_workspace_parser = sub.add_parser("cancel-workspace")
    cancel_workspace_parser.add_argument("workspace")
    args = parser.parse_args()
    if args.version:
        print("cairn-runner 2")
        return 0
    if args.cmd == "execute":
        return execute()
    if args.cmd == "cancel":
        return cancel(args.state_path)
    if args.cmd == "cancel-workspace":
        return cancel_workspace(args.workspace)
    parser.error("missing command")


if __name__ == "__main__":
    raise SystemExit(main())
'''


class SshEnvironment:
    backend = "ssh"

    def __init__(self, config: SshEnvironmentConfig):
        self.id = config.id
        self.label = config.label
        self.config = config
        self.workspace_root = config.workspace_root.rstrip("/")
        self._ssh_argv = config.ssh_argv()
        self.runner_path = config.runner_path or RUNNER_PATH
        self._install_lock = threading.Lock()
        self._installed = False

    def close(self) -> None:
        return

    def prepare_project(self, project_id: str) -> EnvironmentHandle:
        workspace = self._workspace_for(project_id)
        self._ensure_runner()
        self._remote_run(
            [
                "python3",
                "-c",
                "import os,sys;p=sys.argv[1];os.makedirs(p,exist_ok=True)",
                workspace,
            ],
            timeout=10,
        )
        return EnvironmentHandle(project_id=project_id, target_name=self.id, workspace=workspace)

    def prepare_startup(self) -> EnvironmentHandle:
        return self.prepare_project(f".startup-{uuid.uuid4().hex[:12]}")

    def cleanup_startup(self, handle: EnvironmentHandle) -> None:
        self._remove_workspace(handle.workspace)

    def cleanup_key(self, project_id: str) -> str:
        return f"{self.id}:{project_id}"

    def write_text_file(self, handle: EnvironmentHandle, path: str, content: str) -> None:
        workspace = handle.workspace
        if not self.is_path_in_workspace(handle, path):
            raise RuntimeError(f"refusing to write outside ssh workspace: {path}")
        payload = json.dumps({"path": path, "content": content})
        script = (
            "import json,os,sys;"
            "p=json.load(sys.stdin);"
            "os.makedirs(os.path.dirname(p['path']),exist_ok=True);"
            "open(p['path'],'w',encoding='utf-8').write(p['content'])"
        )
        self._remote_run(["python3", "-c", script], input_text=payload, timeout=10)

    def read_text_file(self, handle: EnvironmentHandle, path: str) -> str:
        if not self.is_path_in_workspace(handle, path):
            raise RuntimeError(f"refusing to read outside ssh workspace: {path}")
        result = self._remote_run(["cat", path], timeout=10, check=True)
        return result.stdout

    def delete_file(self, handle: EnvironmentHandle, path: str) -> None:
        if not self.is_path_in_workspace(handle, path):
            raise RuntimeError(f"refusing to delete outside ssh workspace: {path}")
        self._remote_run(["rm", "-f", path], timeout=10, check=False)

    def exists(self, handle: EnvironmentHandle, path: str) -> bool:
        if not self.is_path_in_workspace(handle, path):
            return False
        result = self._remote_run(["test", "-f", path], timeout=5, check=False)
        return result.returncode == 0

    def is_path_in_workspace(self, handle: EnvironmentHandle, path: str) -> bool:
        workspace = posixpath.normpath(handle.workspace)
        target = posixpath.normpath(path)
        return target == workspace or target.startswith(workspace.rstrip("/") + "/")

    def graph_snapshot_path(self, handle: EnvironmentHandle, phase: str) -> str:
        return str(PurePosixPath(handle.workspace) / ".cairn" / "prompts" / phase)

    def build_process(
        self,
        handle: EnvironmentHandle,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
        run_logger: Any | None = None,
    ) -> "SshManagedProcess":
        workspace = handle.workspace
        env = {**env, "CAIRN_WORKSPACE": workspace}
        run_id = getattr(run_logger, "run_id", None) or f"run_{uuid.uuid4().hex}"
        state_path = str(PurePosixPath(workspace) / ".cairn" / "runs" / run_id / "state.json")
        return SshManagedProcess(
            ssh_argv=self._ssh_argv,
            runner_path=self.runner_path,
            workspace=workspace,
            argv=command,
            env=env,
            state_path=state_path,
            timeout_seconds=timeout_seconds,
            kill_after_seconds=kill_after_seconds,
            run_logger=run_logger,
        )

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self.config.cleanup.completed_action == "remove" and self._workspace_exists(project_id)

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return self._workspace_exists(project_id)

    def cleanup_completed(self, project_id: str) -> bool:
        if self.config.cleanup.completed_action != "remove":
            return True
        return self._remove_workspace(self._workspace_for(project_id))

    def cleanup_stopped(self, project_id: str) -> bool:
        workspace = self._workspace_for(project_id)
        self._ensure_runner()
        result = self._remote_run([self.runner_path, "cancel-workspace", workspace], timeout=20, check=False)
        if result.returncode != 0:
            LOG.warning(
                "failed to cancel stopped ssh workspace project=%s workspace=%s stderr=%s",
                project_id,
                workspace,
                _preview(result.stderr or result.stdout),
            )
            return False
        return True

    def run_healthcheck(self, worker_types: Collection[WorkerType] | None = None) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        checks.append(self._check("connect", ["true"], timeout=10))
        if checks[-1]["status"] != "ok":
            return self._health_result(checks)
        probe = f".healthcheck-{uuid.uuid4().hex[:8]}"
        workspace_cmd = [
            "python3",
            "-c",
            "import os,pathlib,sys;"
            "root=pathlib.Path(sys.argv[1]);"
            "root.mkdir(parents=True,exist_ok=True);"
            "p=root/sys.argv[2];p.write_text('ok',encoding='utf-8');"
            "assert p.read_text(encoding='utf-8')=='ok';p.unlink()",
            self.workspace_root,
            probe,
        ]
        checks.append(self._check("workspace", workspace_cmd, timeout=10))
        if checks[-1]["status"] != "ok":
            return self._health_result(checks)
        try:
            self._ensure_runner()
        except Exception as exc:
            checks.append(
                {
                    "name": "runner",
                    "status": "failed",
                    "duration_ms": 0,
                    "command": self._redact_command(["install-runner", self.runner_path]),
                    "stdout": "",
                    "stderr": _preview(str(exc)),
                }
            )
            return self._health_result(checks)
        checks.append(self._check("runner", [self.runner_path, "--version"], timeout=10))
        if checks[-1]["status"] != "ok":
            return self._health_result(checks)
        executables = _required_executables(worker_types)
        if executables:
            checks.append(self._check("worker-cli", _worker_cli_check_command(executables), timeout=10))
        else:
            reason = (
                "Configured worker types do not require remote CLIs."
                if worker_types
                else "No provider endpoints configured; no worker CLI capability declared."
            )
            checks.append(_skipped_check("worker-cli", reason))
        checks.append(self._check("stream", ["sh", "-lc", "printf 'stdout-ok\\n'; printf 'stderr-ok\\n' >&2"], timeout=10))
        if self.config.terminal.mode != "none":
            checks.append(self._check("terminal", ["sh", "-lc", f"command -v {shlex.quote(self.config.terminal.mode)}"], timeout=10))
        return self._health_result(checks)

    def _health_result(self, checks: list[dict[str, Any]]) -> dict[str, Any]:
        status = "ok" if all(check["status"] in {"ok", "skipped"} for check in checks) else "failed"
        return {
            "environment_id": self.id,
            "backend": self.backend,
            "label": self.label,
            "workspace_root": self.workspace_root,
            "status": status,
            "checks": checks,
        }

    def _check(self, name: str, argv: list[str], *, timeout: int) -> dict[str, Any]:
        started = time.perf_counter()
        command_preview = self._redact_command(argv)
        try:
            result = self._remote_run(argv, timeout=timeout, check=False)
        except Exception as exc:
            return {
                "name": name,
                "status": "failed",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "command": command_preview,
                "stdout": "",
                "stderr": _preview(str(exc)),
            }
        return {
            "name": name,
            "status": "ok" if result.returncode == 0 else "failed",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "command": command_preview,
            "stdout": _preview(result.stdout),
            "stderr": _preview(result.stderr),
        }

    def _ensure_runner(self) -> None:
        if self._installed:
            return
        with self._install_lock:
            if self._installed:
                return
            script = (
                "import os,stat,sys;"
                "path=sys.argv[1];"
                "body=sys.stdin.read();"
                "os.makedirs(os.path.dirname(path),exist_ok=True);"
                "open(path,'w',encoding='utf-8').write(body);"
                "os.chmod(path,0o700)"
            )
            self._remote_run(["python3", "-c", script, self.runner_path], input_text=RUNNER_SCRIPT, timeout=10)
            self._installed = True

    def _workspace_for(self, project_id: str) -> str:
        clean = project_id.replace("/", "-").replace("..", "-")
        workspace = str(PurePosixPath(self.workspace_root) / clean)
        self._assert_safe_workspace(workspace)
        return workspace

    def _workspace_exists(self, project_id: str) -> bool:
        workspace = self._workspace_for(project_id)
        result = self._remote_run(["test", "-d", workspace], timeout=5, check=False)
        return result.returncode == 0

    def _remove_workspace(self, workspace: str) -> bool:
        self._assert_safe_workspace(workspace)
        script = (
            "import os,shutil,sys;"
            "root=os.path.realpath(sys.argv[1]);"
            "path=os.path.realpath(sys.argv[2]);"
            "assert path.startswith(root.rstrip('/') + '/'), path;"
            "shutil.rmtree(path, ignore_errors=True)"
        )
        result = self._remote_run(["python3", "-c", script, self.workspace_root, workspace], timeout=20, check=False)
        return result.returncode == 0

    def _assert_safe_workspace(self, workspace: str) -> None:
        root = PurePosixPath(self.workspace_root)
        path = PurePosixPath(workspace)
        if str(root) in {"/", "/home", "/home/kali", "/home/kali/ctf"}:
            raise RuntimeError(f"unsafe ssh workspace root: {root}")
        if str(path) == "/home/kali/ctf" or str(path).startswith("/home/kali/ctf/"):
            raise RuntimeError("refusing to use /home/kali/ctf as workspace")
        if not str(path).startswith(str(root).rstrip("/") + "/") and str(path) != str(root):
            raise RuntimeError(f"workspace outside root: {path}")

    def _remote_run(
        self,
        argv: list[str],
        *,
        input_text: str | None = None,
        timeout: int,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = shlex.join(argv)
        proc: subprocess.CompletedProcess[str] | None = None
        for attempt in range(SSH_REMOTE_RUN_ATTEMPTS):
            proc = subprocess.run(
                [*self._ssh_argv, "-o", "BatchMode=yes", command],
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if not _is_transient_ssh_failure(proc) or attempt >= SSH_REMOTE_RUN_ATTEMPTS - 1:
                break
            LOG.warning(
                "retrying transient ssh command failure environment=%s attempt=%s stderr=%s",
                self.id,
                attempt + 1,
                _preview(proc.stderr or proc.stdout),
            )
            time.sleep(0.2 * (attempt + 1))
        assert proc is not None
        if check and proc.returncode != 0:
            raise RuntimeError(_preview(proc.stderr or proc.stdout or f"ssh command failed: {proc.returncode}"))
        return proc

    def _redact_command(self, argv: list[str]) -> str:
        redacted = []
        for item in argv:
            if item.startswith("sk-") or "API_KEY" in item or "apiKey" in item:
                redacted.append("[redacted]")
            else:
                redacted.append(item)
        return shlex.join([*self._ssh_argv, shlex.join(redacted)])


class SshManagedProcess:
    def __init__(
        self,
        *,
        ssh_argv: list[str],
        runner_path: str,
        workspace: str,
        argv: list[str],
        env: dict[str, str],
        state_path: str,
        timeout_seconds: int | None,
        kill_after_seconds: int,
        run_logger: Any | None,
    ):
        self.ssh_argv = ssh_argv
        self.runner_path = runner_path
        self.workspace = workspace
        self.argv = argv
        self.env = env
        self.state_path = state_path
        self.timeout_seconds = timeout_seconds
        self.kill_after_seconds = kill_after_seconds
        self.run_logger = run_logger
        self._proc: subprocess.Popen[str] | None = None
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []
        self._timed_out = False
        self._cancel_reason: str | None = None

    def start(self) -> None:
        request = {
            "cwd": self.workspace,
            "argv": self.argv,
            "env": self.env,
            "timeout_seconds": self.timeout_seconds,
            "kill_after_seconds": self.kill_after_seconds,
            "state_path": self.state_path,
        }
        remote_command = shlex.join([self.runner_path, "execute"])
        self._proc = subprocess.Popen(
            [*self.ssh_argv, "-o", "BatchMode=yes", remote_command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(request, ensure_ascii=True))
        self._proc.stdin.close()
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        self._threads = [
            threading.Thread(target=self._read_stream, args=(self._proc.stdout, self._stdout, "stdout"), daemon=True),
            threading.Thread(target=self._read_stream, args=(self._proc.stderr, self._stderr, "stderr"), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._proc is not None
        try:
            returncode = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self.kill()
            try:
                returncode = self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                returncode = 137
        for thread in self._threads:
            thread.join(timeout=2)
        return ProcessResult(
            returncode=returncode if returncode is not None else 1,
            stdout="".join(self._stdout),
            stderr="".join(self._stderr),
            timed_out=self._timed_out or returncode == 124,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        subprocess.run(
            [*self.ssh_argv, "-o", "BatchMode=yes", shlex.join([self.runner_path, "cancel", self.state_path])],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()

    def _read_stream(self, stream, target: list[str], name: str) -> None:
        for chunk in iter(stream.readline, ""):
            if not chunk:
                break
            target.append(chunk)
            if self.run_logger is not None:
                self.run_logger.write_stream(name, chunk)


def _required_executables(worker_types: Collection[WorkerType] | None) -> tuple[str, ...]:
    if not worker_types:
        return ()
    executables: set[str] = set()
    for worker_type in worker_types:
        executables.update(get_driver(worker_type).required_executables())
    return tuple(sorted(executables))


def _worker_cli_check_command(executables: tuple[str, ...]) -> list[str]:
    lines = ["set -e"]
    for executable in executables:
        quoted = shlex.quote(executable)
        lines.append(
            f"command -v {quoted} >/dev/null || "
            f"{{ echo 'missing executable: {executable}' >&2; exit 127; }}"
        )
        lines.append(f"{quoted} --version || true")
    return ["sh", "-lc", "\n".join(lines)]


def _preview(text: str) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= SSH_PREVIEW_LIMIT:
        return compact
    return compact[:SSH_PREVIEW_LIMIT] + "..."


def _is_transient_ssh_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode != 255:
        return False
    text = f"{proc.stderr or ''}\n{proc.stdout or ''}"
    return any(marker in text for marker in SSH_TRANSIENT_MARKERS)


def _skipped_check(name: str, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "skipped",
        "duration_ms": 0,
        "command": "-",
        "stdout": "",
        "stderr": reason,
    }
