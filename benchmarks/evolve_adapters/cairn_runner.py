"""CairnRunner — Evolve execution backend that uses Cairn as the rollout engine.

Instead of running a simple ReAct agent (CTFAgent), this runner delegates to
the full Cairn blackboard-architecture system: bootstrap → reason → explore
loop with stigmergy-based multi-agent coordination.

The runner:
  1. Creates a Cairn project via the API
  2. Pre-creates the worker container and injects challenge files
  3. Polls until completion or timeout
  4. Extracts the trajectory from Cairn's fact/intent graph
  5. Returns a RunOutcome compatible with Evolve's grading pipeline

Usage:
    runner = CairnRunner(
        server="http://127.0.0.1:8000",
        admin_token=os.environ["CAIRN_ADMIN_TOKEN"],
        container_image="ghcr.io/oritera/cairn-worker-container:latest",
    )
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

import sys
import os

_EVOLVE_ROOT = os.environ.get("EVOLVE_ROOT", str(Path(__file__).resolve().parents[2].parent / "WYD's Missions" / "Evolve"))
if _EVOLVE_ROOT not in sys.path:
    sys.path.insert(0, _EVOLVE_ROOT)

from flywheel.contract.task import BenchmarkTask, RolloutResult
from flywheel.contract.trajectory import Step, Trajectory
from flywheel.orchestrator.model import Job, JobStatus, RunOutcome
from flywheel.runner.base import Runner

LOG = logging.getLogger(__name__)


class CairnRunner(Runner):
    name = "cairn"

    def __init__(
        self,
        *,
        server: str = "http://127.0.0.1:8000",
        admin_token: str | None = None,
        container_image: str = "ghcr.io/oritera/cairn-worker-container:latest",
        timeout: int = 900,
        poll_interval: int = 10,
        docker_start_timeout: int = 120,
    ) -> None:
        self.server = server
        self.admin_token = os.environ.get("CAIRN_ADMIN_TOKEN", "") if admin_token is None else admin_token
        self.container_image = container_image
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.docker_start_timeout = docker_start_timeout

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.admin_token}",
            "Content-Type": "application/json",
        }

    def _create_project(self, origin: str, goal: str, title: str) -> str:
        resp = requests.post(
            f"{self.server}/projects",
            headers=self._headers(),
            json={"title": title, "origin": origin, "goal": goal, "bootstrap_enabled": True},
        )
        resp.raise_for_status()
        return resp.json()["project"]["id"]

    def _get_project(self, project_id: str) -> dict:
        resp = requests.get(
            f"{self.server}/projects/{project_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def _stop_project(self, project_id: str):
        requests.put(
            f"{self.server}/projects/{project_id}/status",
            headers=self._headers(),
            json={"status": "stopped"},
        )

    def _container_name(self, project_id: str) -> str:
        return f"cairn-dispatch-{project_id.replace('/', '-')}"

    def _precreate_and_inject(self, project_id: str, files_dir: Path):
        name = self._container_name(project_id)
        subprocess.run(
            ["docker", "run", "-d", "--name", name,
             "--network", "host", self.container_image, "sleep", "infinity"],
            capture_output=True, timeout=30,
        )
        for _ in range(10):
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True, text=True,
            )
            if result.stdout.strip() == "true":
                break
            time.sleep(0.5)
        subprocess.run(
            ["docker", "exec", name, "mkdir", "-p", "/tmp/challenge"],
            capture_output=True, timeout=10,
        )
        if any(files_dir.iterdir()):
            subprocess.run(
                ["docker", "cp", f"{files_dir}/.", f"{name}:/tmp/challenge/"],
                capture_output=True, timeout=30,
            )

    def _wait_for_port(self, port: int, timeout: int = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    return True
            except (ConnectionRefusedError, OSError, socket.timeout):
                time.sleep(2)
        return False

    def _poll(self, project_id: str, flag: str) -> tuple[str, list[dict]]:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            data = self._get_project(project_id)
            facts = data.get("facts", [])
            status = data["project"]["status"]

            if any(flag in f.get("description", "") for f in facts):
                return "solved", facts
            if status == "completed":
                return "completed", facts
            if status == "stopped":
                return "stopped", facts
            time.sleep(self.poll_interval)
        return "timeout", []

    def _facts_to_trajectory(
        self, project_id: str, facts: list[dict], intents: list[dict], task_id: str, goal: str,
    ) -> Trajectory:
        steps: list[Step] = []
        step_id = 1
        for intent in intents:
            steps.append(Step(
                step_id=step_id,
                action=f"[intent] {intent.get('description', '')[:500]}",
                observation=None,
                tool_type="cairn_intent",
                metadata={"intent_id": intent.get("id"), "worker": intent.get("worker")},
            ))
            step_id += 1
        for fact in facts:
            if fact["id"] in ("origin", "goal"):
                continue
            steps.append(Step(
                step_id=step_id,
                action=f"[fact] {fact['id']}",
                observation=fact.get("description", ""),
                tool_type="cairn_fact",
            ))
            step_id += 1
        return Trajectory(
            task_id=task_id,
            steps=steps,
            task_description=goal,
            agent="cairn",
        )

    def run(self, job: Job, workdir: Path) -> RunOutcome:
        from .cybench import CybenchAdapter

        adapter = CybenchAdapter()
        tasks = adapter.list_tasks()
        task = next((t for t in tasks if t.task_id == job.instance_id), None)
        if not task:
            return RunOutcome(
                status=JobStatus.Failed,
                metrics={"task_score": 0.0, "passed": False},
                finish_reason=f"task {job.instance_id} not found",
            )

        spec = adapter.local_rollout(task)
        flag = spec.flag or ""
        challenge_dir = Path(task.metadata["challenge_dir"])
        has_docker = task.metadata.get("has_docker", False)

        try:
            # 1. prepare challenge files via init_script
            files_dir = adapter.get_challenge_files_dir(task)

            # 2. start docker if needed
            if has_docker:
                subprocess.run(["docker", "network", "create", "ctfnet"], capture_output=True)
                if spec.run_cmd:
                    subprocess.run(
                        spec.run_cmd, cwd=str(spec.cwd),
                        capture_output=True, text=True,
                        timeout=self.docker_start_timeout,
                    )
                if spec.target:
                    import re
                    m = re.search(r":(\d+)", spec.target)
                    if m:
                        self._wait_for_port(int(m.group(1)))

            # 3. build origin prompt
            origin = adapter.get_init_prompt(task)
            target_host = task.metadata.get("target_host", "")
            if target_host:
                parts = target_host.split(":")
                if len(parts) == 2:
                    origin = origin.replace(target_host, f"localhost:{parts[1]}")
                    origin = origin.replace(parts[0], "localhost")
            origin = origin.strip() + "\nChallenge files are available at /tmp/challenge/"
            goal = "Find and capture the flag."
            title = f"Cybench - {task.metadata.get('name', task.task_id)}"

            # 4. create cairn project
            project_id = self._create_project(origin, goal, title)
            LOG.info("cairn project %s created for %s", project_id, task.task_id)

            # 5. inject files
            self._precreate_and_inject(project_id, files_dir)

            # 6. poll
            start_time = time.time()
            status, facts = self._poll(project_id, flag)
            elapsed = time.time() - start_time

            # 7. build trajectory
            project_data = self._get_project(project_id)
            intents = project_data.get("intents", [])
            traj = self._facts_to_trajectory(
                project_id, facts or project_data.get("facts", []),
                intents, task.task_id, goal,
            )

            solved = any(flag in f.get("description", "") for f in (facts or project_data.get("facts", [])))
            traj.solved = solved

            if status == "timeout":
                self._stop_project(project_id)

            # 8. save trajectory
            traj_path = str(workdir / "steps.json")
            traj.write_steps_json(traj_path)

            return RunOutcome(
                status=JobStatus.Succeeded,
                metrics={
                    "task_score": 1.0 if solved else 0.0,
                    "passed": solved,
                    "category": task.metadata.get("category", ""),
                    "difficulty": task.metadata.get("difficulty"),
                    "num_steps": len(traj),
                    "time_seconds": round(elapsed, 1),
                    "project_id": project_id,
                },
                artifacts_dir=str(workdir),
                trajectory_path=traj_path,
                finish_reason="solved" if solved else status,
            )

        except Exception as exc:
            LOG.exception("cairn runner failed for %s", task.task_id)
            return RunOutcome(
                status=JobStatus.Failed,
                metrics={"task_score": 0.0, "passed": False},
                finish_reason=f"error: {exc}",
            )
        finally:
            if has_docker and spec.stop_cmd:
                try:
                    subprocess.run(
                        spec.stop_cmd, cwd=str(spec.cwd),
                        capture_output=True, timeout=30,
                    )
                except Exception:
                    pass
            if 'files_dir' in dir() and files_dir.exists():
                shutil.rmtree(files_dir, ignore_errors=True)
