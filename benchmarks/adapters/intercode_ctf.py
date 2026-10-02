"""InterCode-CTF adapter — 100 picoCTF challenges (high-school level).

Data layout: data/ctf/ic_ctf.json (task list) + data/ctf/task_assets/{task_id}/ (files).
All challenges are file-based (no Docker networking needed). The agent works on
local files to find the flag.

Source: Yang et al. 2023, "InterCode: Standardizing and Benchmarking Interactive
Coding with Execution Feedback"
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .base import BenchmarkAdapter, ChallengeSpec, register


def _discover_root() -> Path:
    env = os.environ.get("INTERCODE_CTF_ROOT")
    if env:
        return Path(env)
    candidates = [
        Path(__file__).parent.parent / "intercode-ctf",
        Path(__file__).parent.parent / "InterCode-CTF",
        Path.home() / "Documents" / "Obsidian Vault" / "CTF Agent" / "From Harness To Security Dataset" / "open-source-code" / "InterCode-CTF",
    ]
    for c in candidates:
        if (c / "data" / "ctf" / "ic_ctf.json").exists():
            return c
    return candidates[0]


class InterCodeCTFAdapter(BenchmarkAdapter):
    name = "intercode-ctf"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else _discover_root()
        self._tasks_file = self.root / "data" / "ctf" / "ic_ctf.json"
        self._assets_dir = self.root / "data" / "ctf" / "task_assets"
        self._tmp_dirs: dict[str, Path] = {}

    def list_challenges(self, **filters) -> list[dict]:
        if not self._tasks_file.exists():
            raise FileNotFoundError(f"InterCode-CTF data not found at {self._tasks_file}")
        tasks = json.loads(self._tasks_file.read_text())
        challenges = []
        for t in tasks:
            tags = t.get("tags", [])
            category = tags[0].lower().replace(" ", "_") if tags else "unknown"
            challenges.append({
                "challenge_id": str(t["task_id"]),
                "name": f"task_{t['task_id']}",
                "category": category,
                "difficulty": 0,
                "flag": t["gold"],
                "query": t["query"],
                "source": t.get("source", ""),
                "task_id_num": t["task_id"],
            })
        return self.filter_challenges(challenges, **filters)

    def setup(self, challenge: dict) -> ChallengeSpec:
        task_id = str(challenge["task_id_num"])
        asset_dir = self._assets_dir / task_id

        tmp_dir = Path(tempfile.mkdtemp(prefix=f"intercode_ctf_{task_id}_"))
        self._tmp_dirs[challenge["challenge_id"]] = tmp_dir

        if asset_dir.exists():
            for item in asset_dir.iterdir():
                if item.name == "solution":
                    continue
                if item.is_file():
                    shutil.copy2(item, tmp_dir / item.name)
                elif item.is_dir():
                    shutil.copytree(item, tmp_dir / item.name)

        origin = challenge["query"].strip()
        origin += "\nChallenge files are available at /tmp/challenge/"

        return ChallengeSpec(
            challenge_id=challenge["challenge_id"],
            name=challenge["name"],
            category=challenge.get("category", ""),
            difficulty=0,
            origin=origin,
            goal="Find and capture the flag.",
            flag=challenge["flag"],
            files_dir=tmp_dir,
        )

    def teardown(self, challenge: dict) -> None:
        tmp_dir = self._tmp_dirs.pop(challenge.get("challenge_id", ""), None)
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


register(InterCodeCTFAdapter())
