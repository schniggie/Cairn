"""Cybench adapter for Evolve — maps 40 professional CTF challenges into the
unified BenchmarkAdapter contract.

Reads the Cybench challenge tree (GLA/HKC/HTB/S22/S23) and exposes each
challenge as a BenchmarkTask. Supports local rollout via docker-compose.

Usage with Evolve orchestrator:
    from benchmarks.evolve_adapters.cybench import CybenchAdapter
    adapter = CybenchAdapter("/path/to/benchmarks/cybench")
    tasks = adapter.list_tasks()
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import sys

_EVOLVE_ROOT = os.environ.get("EVOLVE_ROOT", str(Path(__file__).resolve().parents[2].parent / "WYD's Missions" / "Evolve"))
if _EVOLVE_ROOT not in sys.path:
    sys.path.insert(0, _EVOLVE_ROOT)

from flywheel.adapters.base import BenchmarkAdapter, LocalRolloutSpec, register
from flywheel.contract.task import BenchmarkTask
from flywheel.contract.trajectory import Trajectory


def _discover_root() -> Path:
    env = os.environ.get("CYBENCH_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    # benchmarks/evolve_adapters/cybench.py -> benchmarks/cybench
    candidate = here.parent.parent / "cybench"
    if candidate.is_dir():
        return candidate
    return Path.cwd() / "benchmarks" / "cybench"


class CybenchAdapter(BenchmarkAdapter):
    name = "cybench"
    flag_pattern = r"(?:HTB|FLAG|flag|ctf|CTF|CSAW|hkcert)\{[^}]{1,200}\}"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else _discover_root()

    def list_tasks(self, *, split: str = "") -> list[BenchmarkTask]:
        tasks: list[BenchmarkTask] = []
        for cj in sorted(self.root.rglob("challenge.json")):
            challenge_dir = cj.parent
            rel = challenge_dir.relative_to(self.root)
            if len(rel.parts) != 3:
                continue
            data = json.loads(cj.read_text())
            competition, category, cname = rel.parts
            has_docker = (challenge_dir / "docker-compose.yml").exists()
            tasks.append(BenchmarkTask(
                task_id=f"{competition}/{category}/{cname}",
                benchmark=self.name,
                dataset="cybench",
                split=split,
                goal=data.get("hard_prompt", data.get("description", "")),
                metadata={
                    "name": data.get("name", cname),
                    "category": data.get("category", category).lower(),
                    "difficulty": int(data.get("difficulty", -1)),
                    "flag": data["flag"],
                    "target_host": data.get("target_host", ""),
                    "internal_port": data.get("internal_port"),
                    "files": data.get("files", []),
                    "has_docker": has_docker,
                    "challenge_dir": str(challenge_dir),
                },
            ))
        return tasks

    def flag_for(self, task: BenchmarkTask) -> str | None:
        return task.metadata.get("flag")

    def verify(self, task: BenchmarkTask, trajectory: Trajectory) -> bool:
        flag = self.flag_for(task)
        if not flag:
            return False
        for s in trajectory.steps:
            if (s.action and flag in s.action) or (s.observation and flag in s.observation):
                return True
        return False

    def local_rollout(self, task: BenchmarkTask) -> LocalRolloutSpec:
        challenge_dir = Path(task.metadata["challenge_dir"])
        has_docker = task.metadata.get("has_docker", False)
        target_host = task.metadata.get("target_host", "")
        internal_port = task.metadata.get("internal_port")

        build_cmd: list[str] = []
        run_cmd: list[str] = []
        stop_cmd: list[str] = []

        if has_docker:
            run_cmd = ["bash", str(challenge_dir / "start_docker.sh")]
            stop_cmd = ["bash", str(challenge_dir / "stop_docker.sh")]

        target = ""
        if target_host:
            parts = target_host.split(":")
            if len(parts) == 2:
                target = f"http://localhost:{parts[1]}"

        return LocalRolloutSpec(
            cwd=challenge_dir,
            build_cmd=build_cmd,
            run_cmd=run_cmd,
            stop_cmd=stop_cmd,
            target=target,
            flag=task.metadata.get("flag"),
            category=task.metadata.get("category", ""),
            extra={
                "has_docker": has_docker,
                "internal_port": internal_port,
                "challenge_dir": str(challenge_dir),
                "difficulty": task.metadata.get("difficulty"),
            },
        )

    def get_init_prompt(self, task: BenchmarkTask) -> str:
        challenge_dir = Path(task.metadata["challenge_dir"])
        init_script = challenge_dir / "init_script.sh"
        if not init_script.exists():
            return task.goal

        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="cybench_init_")
        try:
            result = subprocess.run(
                ["bash", str(init_script), tmp_dir, "false"],
                capture_output=True, text=True, timeout=120,
                cwd=str(challenge_dir),
            )
            prompt = result.stdout.strip()
            return prompt if prompt else task.goal
        except Exception:
            return task.goal

    def get_challenge_files_dir(self, task: BenchmarkTask) -> Path:
        challenge_dir = Path(task.metadata["challenge_dir"])
        init_script = challenge_dir / "init_script.sh"

        import tempfile
        tmp_dir = Path(tempfile.mkdtemp(prefix="cybench_files_"))
        if init_script.exists():
            subprocess.run(
                ["bash", str(init_script), str(tmp_dir), "false"],
                capture_output=True, text=True, timeout=120,
                cwd=str(challenge_dir),
            )
        return tmp_dir
