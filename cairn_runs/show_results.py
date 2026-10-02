#!/usr/bin/env python3
"""Show aggregated xbow benchmark results across all batch runs."""
import json
import sys
from pathlib import Path

# Results live in .cairn_runs/results/ (sibling of cairn_runs/)
RESULTS_DIR = Path(__file__).parent.parent / ".cairn_runs" / "results"


def main():
    # Collect all TSV results; priority-based merging per benchmark
    results = {}  # bench_id -> {status, passed, found_flag, expected_flag, duration, project_id, source_file}

    for tsv in sorted(RESULTS_DIR.glob("batch_*.tsv")):
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
                "found_flag": cols[3] if len(cols) > 3 else "",
                "expected_flag": cols[4] if len(cols) > 4 else "",
                "duration": cols[5] if len(cols) > 5 else "?",
                "project_id": cols[6] if len(cols) > 6 else "",
                "source": tsv.name,
            }
            # Priority: PASS > completed/active > start_failed
            if bench in results:
                old_r = results[bench]
                if old_r["passed"]:
                    continue  # never overwrite PASS
                if old_r["status"] in ("completed", "active") and new_entry["status"] == "start_failed":
                    continue  # keep real result over build failure
            results[bench] = new_entry

    if not results:
        print("No results found.")
        return

    # Sort by benchmark number
    def sort_key(k):
        try:
            return int(k.split("-")[1])
        except (IndexError, ValueError):
            return 999

    sorted_benches = sorted(results.keys(), key=sort_key)

    # Stats
    total = len(sorted_benches)
    passed = sum(1 for r in results.values() if r["passed"])
    failed = sum(1 for r in results.values() if not r["passed"])
    timeout_count = sum(1 for r in results.values() if r["status"] == "active")

    # Check trajectories
    traj_dir = RESULTS_DIR / "trajectories"
    traj_count = len(list(traj_dir.glob("*.json"))) if traj_dir.exists() else 0

    # Header
    print("=" * 70)
    print(f"  XBOW Validation Results Summary")
    print(f"  Total: {total}/104 | PASS: {passed} | FAIL: {failed} | Rate: {passed/max(1,total)*100:.1f}%")
    print(f"  Timeouts: {timeout_count} | Trajectories saved: {traj_count}")
    print("=" * 70)
    print()
    print(f"  {'#':<4} {'Benchmark':<14} {'Result':<8} {'Status':<12} {'Time':>6}  {'Note'}")
    print(f"  {'─'*4} {'─'*14} {'─'*8} {'─'*12} {'─'*6}  {'─'*20}")

    for i, bench in enumerate(sorted_benches, 1):
        r = results[bench]
        is_timeout = r["status"] in ("active", "timeout")
        if r["passed"]:
            mark = "\033[32m PASS \033[0m"
        elif is_timeout:
            mark = "\033[33mTIMEOUT\033[0m"
        else:
            mark = "\033[31m FAIL \033[0m"

        dur = r["duration"]
        note = ""
        if not r["passed"] and r["found_flag"] and r["status"] == "completed":
            note = "wrong flag"
        elif is_timeout:
            note = "timeout"
        elif r["status"] == "start_failed":
            note = "build error"
        elif r["status"] == "register_failed":
            note = "register error"

        # Check trajectory exists
        has_traj = "T" if (traj_dir / f"{bench}.json").exists() else " "

        print(f"  {i:<4} {bench:<14} {mark} {r['status']:<12} {dur:>5}s  {has_traj} {note}")

    # Summary by category
    print()
    print("─" * 70)
    print(f"  PASS: {passed}/{total} ({passed/max(1,total)*100:.1f}%)")

    # Still running check
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-lf", "run_xbow_batch"], capture_output=True, text=True, timeout=3)
        if out.stdout.strip():
            print(f"  ⏳ Batch still running (check: tail -f .cairn_runs/results/batch_*.log)")
    except Exception:
        pass

    # Remaining
    all_benches = set(f"XBEN-{i:03d}-24" for i in range(1, 105))
    done_benches = set(results.keys())
    remaining = all_benches - done_benches
    if remaining:
        print(f"  Remaining: {len(remaining)} benchmarks not yet attempted")


if __name__ == "__main__":
    main()
