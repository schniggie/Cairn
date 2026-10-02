"""AutoPenBench adapter — 33 realistic penetration testing scenarios.

Data layout:
  data/games.json              — task definitions (split by in-vitro / real-world)
  benchmark/machines/          — docker-compose + VM Dockerfiles
  benchmark/machines/kali/     — Kali attacker workstation

All challenges require Docker networking. The benchmark creates a private
192.168.x.0/24 subnet per category. The agent attacks from a Kali container.

Key difference from CTF benchmarks: AutoPenBench requires network discovery
(nmap scan of a /24 subnet), not just attacking a known host:port.

Source: Gioacchini et al. 2024, "AutoPenBench: Benchmarking Generative Agents
for Penetration Testing"
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .base import BenchmarkAdapter, ChallengeSpec, register

_DOCKER_TIMEOUT = 300


def _discover_root() -> Path:
    env = os.environ.get("AUTOPENBENCH_ROOT")
    if env:
        return Path(env)
    candidates = [
        Path(__file__).parent.parent / "autopenbench",
        Path(__file__).parent.parent / "AutoPenBench",
        Path.home() / "Documents" / "Obsidian Vault" / "CTF Agent" / "From Harness To Security Dataset" / "open-source-code" / "AutoPenBench",
    ]
    for c in candidates:
        if (c / "data" / "games.json").exists():
            return c
    return candidates[0]


class AutoPenBenchAdapter(BenchmarkAdapter):
    name = "autopenbench"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else _discover_root()
        self._games_file = self.root / "data" / "games.json"
        self._machines_dir = self.root / "benchmark" / "machines"

    def list_challenges(self, **filters) -> list[dict]:
        if not self._games_file.exists():
            raise FileNotFoundError(f"AutoPenBench data not found at {self._games_file}")
        data = json.loads(self._games_file.read_text())
        challenges = []
        idx = 0
        for split_name, split_data in data.items():
            for cat_name, tasks in split_data.items():
                for task in tasks:
                    challenge_id = f"{split_name}/{cat_name}/{task['target']}"
                    difficulty = 2 if split_name == "real-world" else 1
                    challenges.append({
                        "challenge_id": challenge_id,
                        "name": task["target"],
                        "category": cat_name,
                        "difficulty": difficulty,
                        "flag": task["flag"],
                        "task_description": task["task"],
                        "target": task["target"],
                        "vulnerability": task.get("vulnerability", ""),
                        "alias": task.get("alias", ""),
                        "split": split_name,
                        "compose_dir": str(self._get_compose_dir(split_name, cat_name)),
                    })
                    idx += 1
        return self.filter_challenges(challenges, **filters)

    def setup(self, challenge: dict) -> ChallengeSpec:
        compose_dir = Path(challenge["compose_dir"])

        # start the target VM + kali attacker via docker-compose
        if compose_dir.exists() and (compose_dir / "docker-compose.yml").exists():
            # build kali first if needed
            kali_dir = self._machines_dir / "kali"
            if kali_dir.exists():
                subprocess.run(
                    ["docker", "compose", "build", "kali_master"],
                    cwd=str(self._machines_dir),
                    capture_output=True, timeout=_DOCKER_TIMEOUT,
                )
            # build + start the target VM
            target_name = challenge["target"]
            subprocess.run(
                ["docker", "compose", "build", target_name],
                cwd=str(compose_dir),
                capture_output=True, timeout=_DOCKER_TIMEOUT,
            )
            subprocess.run(
                ["docker", "compose", "up", "-d", target_name],
                cwd=str(compose_dir),
                capture_output=True, timeout=_DOCKER_TIMEOUT,
            )

        origin = challenge["task_description"].strip()
        origin += "\nChallenge files are available at /tmp/challenge/"

        return ChallengeSpec(
            challenge_id=challenge["challenge_id"],
            name=challenge["name"],
            category=challenge.get("category", ""),
            difficulty=challenge.get("difficulty", 1),
            origin=origin,
            goal="Find and capture the flag.",
            flag=challenge["flag"],
            files_dir=None,
            metadata=challenge,
        )

    def teardown(self, challenge: dict) -> None:
        compose_dir = Path(challenge.get("compose_dir", ""))
        if compose_dir.exists() and (compose_dir / "docker-compose.yml").exists():
            target_name = challenge.get("target", "")
            if target_name:
                subprocess.run(
                    ["docker", "compose", "down", target_name],
                    cwd=str(compose_dir),
                    capture_output=True, timeout=60,
                )

    def _get_compose_dir(self, split_name: str, cat_name: str) -> Path:
        return self._machines_dir / split_name / cat_name


register(AutoPenBenchAdapter())
