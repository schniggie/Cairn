#!/usr/bin/env python3
"""Compare Cairn vs Flywheel (Claude Code) results on XBOW benchmarks.

Environment variables:
    FLYWHEEL_JOBS_DIR     (required if comparing) Path to flywheel-jobs/ directory
    VALIDATION_BENCHMARKS_DIR  (optional) Path to validation-benchmarks/benchmarks

Usage:
    export FLYWHEEL_JOBS_DIR="/path/to/flywheel-jobs"
    python3 compare_flywheel.py
"""
import json
import re
import hashlib
import os
import sys
from pathlib import Path


def _find_flywheel_dir() -> Path:
    env = os.environ.get("FLYWHEEL_JOBS_DIR")
    if env:
        return Path(env)
    sys.exit("ERROR: FLYWHEEL_JOBS_DIR environment variable is required.\n"
             "  export FLYWHEEL_JOBS_DIR='/path/to/flywheel-jobs'")


def _find_benchmarks_dir() -> Path:
    env = os.environ.get("VALIDATION_BENCHMARKS_DIR")
    if env:
        return Path(env)
    cairn_root = Path(__file__).parent.parent
    sibling = cairn_root.parent / "validation-benchmarks" / "benchmarks"
    if sibling.exists():
        return sibling
    sys.exit("ERROR: Cannot find validation-benchmarks/benchmarks.\n"
             "  Set VALIDATION_BENCHMARKS_DIR or place it alongside the Cairn/ directory.")


FLYWHEEL_DIR = _find_flywheel_dir()
CAIRN_RESULTS_DIR = Path(__file__).parent.parent / ".cairn_runs" / "results"
BENCHMARKS_DIR = _find_benchmarks_dir()


def get_expected_flag(benchmark_id):
    return "FLAG{" + hashlib.sha256(benchmark_id.upper().encode()).hexdigest() + "}"


def get_cairn_results():
    """Get Cairn results from TSV files."""
    results = {}
    for tsv in sorted(CAIRN_RESULTS_DIR.glob("batch_*.tsv")):
        for line in tsv.read_text().strip().split("\n")[1:]:
            if not line.strip():
                continue
            cols = line.split("\t")
            if len(cols) < 6:
                continue
            bench = cols[0]
            new_entry = {
                "status": cols[1],
                "passed": cols[2] == "True",
                "duration": int(cols[5]) if cols[5].isdigit() else 0,
            }
            if bench in results:
                old = results[bench]
                if old["passed"]:
                    continue
                if old["status"] in ("completed", "active") and new_entry["status"] == "start_failed":
                    continue
            results[bench] = new_entry
    return results


def get_flywheel_results():
    """Parse flywheel job results."""
    results = {}

    for job_dir in sorted(FLYWHEEL_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        steps_file = job_dir / "steps.json"
        if not steps_file.exists():
            continue
        steps = json.loads(steps_file.read_text())

        benchmark = None
        flag_found = None
        total_steps = len(steps)

        for step in steps:
            obs = step.get("observation", "") or ""
            action = step.get("action", "") or ""
            combined = obs + action

            if not benchmark:
                m = re.search(r"XBEN-(\d+)-24", combined)
                if m:
                    benchmark = f"XBEN-{int(m.group(1)):03d}-24"

            fm = re.search(r"FLAG\{[a-f0-9]{64}\}", combined)
            if fm:
                flag_found = fm.group(0)

        if not benchmark:
            # Try to match flag to determine benchmark
            if flag_found:
                for i in range(1, 105):
                    bid = f"XBEN-{i:03d}-24"
                    if flag_found == get_expected_flag(bid):
                        benchmark = bid
                        break

        if benchmark:
            passed = False
            if flag_found:
                expected = get_expected_flag(benchmark)
                passed = (flag_found == expected)

            # Keep best result per benchmark
            if benchmark in results:
                if results[benchmark]["passed"]:
                    continue

            results[benchmark] = {
                "passed": passed,
                "flag_found": flag_found,
                "steps": total_steps,
                "job": job_dir.name,
            }

    return results


def main():
    cairn = get_cairn_results()
    flywheel = get_flywheel_results()

    print("=" * 70)
    print("  Cairn (qwen3.7-max) vs Flywheel (Claude Code) 对比")
    print("=" * 70)

    cairn_pass = sum(1 for r in cairn.values() if r["passed"])
    fw_pass = sum(1 for r in flywheel.values() if r["passed"])

    print(f"\n  Cairn:    {cairn_pass}/104 PASS")
    print(f"  Flywheel: {fw_pass}/104 PASS ({len(flywheel)} benchmarks identified)")

    # Cairn PASS but Flywheel FAIL
    cairn_only = []
    for i in range(1, 105):
        bid = f"XBEN-{i:03d}-24"
        c = cairn.get(bid, {})
        f = flywheel.get(bid, {})
        if c.get("passed") and not f.get("passed"):
            cairn_only.append((bid, c.get("duration", 0), f.get("steps", "N/A")))

    # Flywheel PASS but Cairn FAIL
    flywheel_only = []
    for i in range(1, 105):
        bid = f"XBEN-{i:03d}-24"
        c = cairn.get(bid, {})
        f = flywheel.get(bid, {})
        if f.get("passed") and not c.get("passed"):
            flywheel_only.append((bid, c.get("duration", 0), c.get("status", "N/A"), f.get("steps", 0)))

    # Both PASS
    both_pass = []
    for i in range(1, 105):
        bid = f"XBEN-{i:03d}-24"
        c = cairn.get(bid, {})
        f = flywheel.get(bid, {})
        if c.get("passed") and f.get("passed"):
            both_pass.append(bid)

    print(f"\n  两者都通过: {len(both_pass)} 题")
    print(f"  仅 Cairn 通过: {len(cairn_only)} 题")
    print(f"  仅 Flywheel 通过: {len(flywheel_only)} 题")

    if cairn_only:
        print(f"\n{'─' * 70}")
        print(f"  Cairn 做出来但 Flywheel 没做出来 ({len(cairn_only)} 题):")
        print(f"  {'Benchmark':<14} {'Cairn用时':>10} {'FW Steps':>10}")
        for bid, dur, fw_steps in sorted(cairn_only):
            print(f"  {bid:<14} {dur:>8}s  {fw_steps:>8}")

    if flywheel_only:
        print(f"\n{'─' * 70}")
        print(f"  Flywheel 做出来但 Cairn 没做出来 ({len(flywheel_only)} 题):")
        print(f"  {'Benchmark':<14} {'Cairn状态':<12} {'Cairn用时':>10} {'FW Steps':>10}")
        for bid, dur, status, fw_steps in sorted(flywheel_only):
            print(f"  {bid:<14} {status:<12} {dur:>8}s  {fw_steps:>8}")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()
