"""Cybench adapter — 40 professional CTF challenges.

Challenge layout: benchmarks/cybench/{competition}/{category}/{name}/
Each has: challenge.json, init_script.sh, optionally docker-compose.yml + start/stop_docker.sh.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .base import BenchmarkAdapter, ChallengeSpec, register

_DOCKER_START_TIMEOUT = 120
_PORT_WAIT_TIMEOUT = 60


class CybenchAdapter(BenchmarkAdapter):
    name = "cybench"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else Path(__file__).parent.parent / "cybench"
        self._tmp_dirs: dict[str, Path] = {}

    def list_challenges(self, **filters) -> list[dict]:
        challenges = []
        for cj in sorted(self.root.rglob("challenge.json")):
            challenge_dir = cj.parent
            rel = challenge_dir.relative_to(self.root)
            if len(rel.parts) != 3:
                continue
            data = json.loads(cj.read_text())
            competition, category, cname = rel.parts
            challenges.append({
                "challenge_id": f"{competition}/{category}/{cname}",
                "name": data.get("name", cname),
                "category": data.get("category", category).lower(),
                "difficulty": int(data.get("difficulty", -1)),
                "flag": data["flag"],
                "target_host": data.get("target_host", ""),
                "internal_port": data.get("internal_port"),
                "has_docker": (challenge_dir / "docker-compose.yml").exists(),
                "challenge_dir": str(challenge_dir),
            })
        return self.filter_challenges(challenges, **filters)

    def setup(self, challenge: dict) -> ChallengeSpec:
        challenge_dir = Path(challenge["challenge_dir"])
        data = json.loads((challenge_dir / "challenge.json").read_text())
        distributed_files = data.get("files", [])

        # 1. copy only officially distributed files (what CTF participants received)
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"cybench_{challenge['name'].replace(' ', '_')}_"))
        self._tmp_dirs[challenge["challenge_id"]] = tmp_dir

        if distributed_files:
            for fpath in distributed_files:
                src = challenge_dir / fpath
                if not src.exists():
                    continue
                if src.is_dir():
                    dest = tmp_dir / src.name
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                else:
                    dest = tmp_dir / src.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)

        # 2. get prompt (hard_prompt from challenge.json, not init_script noise)
        prompt = self._get_prompt(data)

        # 3. start docker if needed
        if challenge.get("has_docker"):
            self._ensure_ctfnet()
            self._start_docker(challenge_dir)
            port = self._extract_host_port(challenge_dir, challenge.get("internal_port"))
            if port:
                self._wait_for_port(port)

        # 4. rewrite hostnames to localhost
        target_host = challenge.get("target_host", "")
        if target_host:
            parts = target_host.split(":")
            if len(parts) == 2:
                prompt = prompt.replace(target_host, f"localhost:{parts[1]}")
                prompt = prompt.replace(parts[0], "localhost")

        # 5. only mention files if we actually injected some
        origin = prompt.strip()
        if distributed_files:
            origin += "\nChallenge files are available at /tmp/challenge/"

        return ChallengeSpec(
            challenge_id=challenge["challenge_id"],
            name=challenge["name"],
            category=challenge.get("category", ""),
            difficulty=challenge.get("difficulty", -1),
            origin=origin,
            goal="Find and capture the flag.",
            flag=challenge["flag"],
            files_dir=tmp_dir if distributed_files else None,
            metadata=challenge,
        )

    def teardown(self, challenge: dict) -> None:
        challenge_dir = Path(challenge["challenge_dir"])
        if challenge.get("has_docker"):
            stop_script = challenge_dir / "stop_docker.sh"
            if stop_script.exists():
                subprocess.run(
                    ["bash", str(stop_script)], cwd=str(challenge_dir),
                    capture_output=True, timeout=30,
                )
        tmp_dir = self._tmp_dirs.pop(challenge.get("challenge_id", ""), None)
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _get_prompt(data: dict) -> str:
        """Get the agent prompt from challenge.json. Use hard_prompt (minimal info,
        no vulnerability hints). Fall back to description if hard_prompt is empty."""
        return data.get("hard_prompt", "") or data.get("description", "")

    @staticmethod
    def _ensure_ctfnet():
        subprocess.run(["docker", "network", "create", "ctfnet"], capture_output=True)

    @staticmethod
    def _start_docker(challenge_dir: Path):
        start_script = challenge_dir / "start_docker.sh"
        if start_script.exists():
            subprocess.run(
                ["bash", str(start_script)], cwd=str(challenge_dir),
                capture_output=True, text=True, timeout=_DOCKER_START_TIMEOUT,
            )

    @staticmethod
    def _extract_host_port(challenge_dir: Path, internal_port: int | None = None) -> int | None:
        compose_file = challenge_dir / "docker-compose.yml"
        if not compose_file.exists():
            return None
        text = compose_file.read_text()
        m = re.search(r'["\']?(\d+):\d+["\']?', text)
        if m:
            return int(m.group(1))
        return internal_port

    @staticmethod
    def _wait_for_port(port: int, timeout: int = _PORT_WAIT_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    return
            except (ConnectionRefusedError, OSError, socket.timeout):
                time.sleep(2)


register(CybenchAdapter())
