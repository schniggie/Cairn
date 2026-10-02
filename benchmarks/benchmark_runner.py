#!/usr/bin/env python3
"""Universal benchmark runner for Cairn.

Runs any benchmark through Cairn by delegating to a benchmark adapter.
The adapter handles all benchmark-specific logic (discovery, setup, teardown,
verification). The runner handles all Cairn-specific logic (project creation,
file injection via init_files API, polling, result collection).

Usage:
  python3 benchmarks/benchmark_runner.py cybench
  python3 benchmarks/benchmark_runner.py cybench --category web
  python3 benchmarks/benchmark_runner.py cybench --challenge "Labyrinth Linguist"
  python3 benchmarks/benchmark_runner.py cybench --timeout 1200 --difficulty 0
  python3 benchmarks/benchmark_runner.py --list-adapters
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from adapters import get_adapter, list_adapters
from adapters.base import ChallengeSpec

# ── defaults ────────────────────────────────────────────────────────────

CAIRN_SERVER = "http://127.0.0.1:8000"
ADMIN_TOKEN = os.environ.get("CAIRN_ADMIN_TOKEN", "")
TIMEOUT_SECONDS = 900
POLL_INTERVAL = 10

LOG = logging.getLogger("benchmark_runner")


def _update_globals(server: str, token: str):
    global CAIRN_SERVER, ADMIN_TOKEN
    CAIRN_SERVER = server
    ADMIN_TOKEN = token


# ── result model ────────────────────────────────────────────────────────


@dataclass
class Result:
    benchmark: str
    challenge_id: str
    name: str
    category: str
    difficulty: int
    status: str
    flag_expected: str
    flag_found: bool
    time_seconds: float
    project_id: str
    error_message: str = ""


# ── cairn API ───────────────────────────────────────────────────────────


def _headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"}


def create_project(origin: str, goal: str, title: str, files_dir: Path | None = None) -> str:
    import base64
    init_files = []
    if files_dir and files_dir.exists() and any(files_dir.iterdir()):
        for f in sorted(files_dir.rglob("*")):
            if not f.is_file():
                continue
            rel_path = f"/tmp/challenge/{f.relative_to(files_dir)}"
            try:
                content = f.read_text(encoding="utf-8")
                encoding = "utf-8"
            except (UnicodeDecodeError, ValueError):
                content = base64.b64encode(f.read_bytes()).decode()
                encoding = "base64"
            init_files.append({"path": rel_path, "content": content, "encoding": encoding})

    body = {"title": title, "origin": origin, "goal": goal, "bootstrap_enabled": True}
    if init_files:
        body["init_files"] = init_files
    resp = requests.post(f"{CAIRN_SERVER}/projects", headers=_headers(), json=body)
    resp.raise_for_status()
    return resp.json()["project"]["id"]


def get_project(project_id: str) -> dict:
    resp = requests.get(f"{CAIRN_SERVER}/projects/{project_id}", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def stop_project(project_id: str):
    requests.put(
        f"{CAIRN_SERVER}/projects/{project_id}/status", headers=_headers(),
        json={"status": "stopped"},
    )




# ── polling ─────────────────────────────────────────────────────────────


def poll_project(project_id: str, flag: str, timeout: int, adapter, challenge: dict) -> tuple[str, list[dict]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = get_project(project_id)
        facts = data.get("facts", [])
        status = data["project"]["status"]

        if adapter.verify(challenge, facts):
            return "solved", facts
        if status == "completed":
            return "completed", facts
        if status == "stopped":
            return "stopped", facts
        time.sleep(POLL_INTERVAL)
    return "timeout", []


# ── results persistence ─────────────────────────────────────────────────


def results_file(benchmark_name: str) -> Path:
    return Path(__file__).parent / f"{benchmark_name}_results" / "results.json"


def load_results(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_results(path: Path, results: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False))


def completed_ids(results: list[dict]) -> set[str]:
    return {r["challenge_id"] for r in results if r["status"] in ("solved", "unsolved", "timeout")}


# ── run one challenge ───────────────────────────────────────────────────


def run_one(adapter, challenge: dict, timeout: int) -> Result:
    name = challenge.get("name", challenge["challenge_id"])
    category = challenge.get("category", "")
    difficulty = challenge.get("difficulty", -1)

    LOG.info("═" * 60)
    LOG.info("START  %s  [%s, difficulty=%s]", name, category, difficulty)

    start_time = time.time()
    spec: ChallengeSpec | None = None

    try:
        spec = adapter.setup(challenge)

        project_id = create_project(spec.origin, spec.goal, f"{adapter.name} - {spec.name} ({spec.category})", spec.files_dir)
        LOG.info("created project %s", project_id)

        status, facts = poll_project(project_id, spec.flag, timeout, adapter, challenge)

        elapsed = time.time() - start_time
        flag_found = adapter.verify(challenge, facts)

        if flag_found:
            final_status = "solved"
        elif status == "completed":
            final_status = "unsolved"
        elif status == "timeout":
            stop_project(project_id)
            final_status = "timeout"
        else:
            final_status = "unsolved"

        LOG.info("RESULT %s  status=%s  flag_found=%s  time=%.1fs", name, final_status, flag_found, elapsed)

        return Result(
            benchmark=adapter.name, challenge_id=challenge["challenge_id"],
            name=name, category=category, difficulty=difficulty,
            status=final_status, flag_expected=spec.flag, flag_found=flag_found,
            time_seconds=round(elapsed, 1), project_id=project_id,
        )

    except Exception as exc:
        LOG.exception("challenge %s crashed", name)
        return Result(
            benchmark=adapter.name, challenge_id=challenge["challenge_id"],
            name=name, category=category, difficulty=difficulty,
            status="error", flag_expected=challenge.get("flag", ""), flag_found=False,
            time_seconds=round(time.time() - start_time, 1), project_id="",
            error_message=str(exc),
        )
    finally:
        adapter.teardown(challenge)


# ── main ────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Universal benchmark runner for Cairn")
    parser.add_argument("benchmark", nargs="?", help="benchmark adapter name (e.g. cybench)")
    parser.add_argument("--list-adapters", action="store_true", help="list available benchmark adapters")
    parser.add_argument("--category", help="filter by category")
    parser.add_argument("--difficulty", type=int, help="filter by difficulty level")
    parser.add_argument("--challenge", help="filter by challenge name (substring match)")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS, help=f"per-challenge timeout (default: {TIMEOUT_SECONDS}s)")
    parser.add_argument("--server", default=CAIRN_SERVER, help="Cairn server URL")
    parser.add_argument("--token", default=ADMIN_TOKEN, help="Cairn admin token")
    parser.add_argument("--reset", action="store_true", help="ignore previous results")
    args = parser.parse_args()

    if args.list_adapters:
        print("Available adapters:", ", ".join(list_adapters()))
        return

    if not args.benchmark:
        parser.error("benchmark name required (use --list-adapters to see options)")

    _update_globals(args.server, args.token)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    adapter = get_adapter(args.benchmark)
    filters = {}
    if args.category:
        filters["category"] = args.category
    if args.difficulty is not None:
        filters["difficulty"] = args.difficulty
    if args.challenge:
        filters["challenge"] = args.challenge

    challenges = adapter.list_challenges(**filters)
    LOG.info("[%s] discovered %d challenges", adapter.name, len(challenges))

    rf = results_file(adapter.name)
    results = [] if args.reset else load_results(rf)
    done = completed_ids(results)

    solved = sum(1 for r in results if r["status"] == "solved")
    total_run = len(results)

    for i, challenge in enumerate(challenges, 1):
        if challenge["challenge_id"] in done:
            LOG.info("SKIP   %s (already done)", challenge.get("name", challenge["challenge_id"]))
            continue

        LOG.info("[%d/%d] %s", i, len(challenges), challenge["challenge_id"])
        result = run_one(adapter, challenge, args.timeout)
        results.append(asdict(result))
        save_results(rf, results)

        if result.status == "solved":
            solved += 1
        total_run += 1
        LOG.info("running score: %d/%d solved (%.1f%%)", solved, total_run, solved / total_run * 100 if total_run else 0)

    LOG.info("═" * 60)
    LOG.info("FINAL: %d/%d solved (%.1f%%)", solved, total_run, solved / total_run * 100 if total_run else 0)

    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, cat_results in sorted(by_cat.items()):
        cat_solved = sum(1 for r in cat_results if r["status"] == "solved")
        LOG.info("  %s: %d/%d", cat, cat_solved, len(cat_results))


if __name__ == "__main__":
    main()
